"""Tests for the StreamSequencer (ISSUE_9) — needs a reachable Postgres (skipped otherwise).

The unit exists to keep one promise to a downstream consumer: **a gap in `seq` means exactly one
thing — a record that never arrived.** Everything here either proves that promise or proves the
guard that keeps it true across a restore, so the tests are written against the promise rather
than against the implementation.

Runs on the canonical `stream_seq` table inside the migration-built test schema (`clean_db`), so a
migration that drifts from the code fails here instead of hiding behind test DDL.
"""
import json
import threading
from datetime import datetime, timezone

import psycopg
import pytest

from finiexragengine.core.outcome.stream_sequencer import StreamSequencer

_NOW = int(datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture
def sequencer(clean_db: str) -> StreamSequencer:
    return StreamSequencer(clean_db)


def _mint(dsn: str, sequencer: StreamSequencer, pipeline_id: str = 'p', now_msc: int = _NOW):
    """One mint in its own committed transaction — the shape `OutcomeStore.save` uses."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        return sequencer.mint(cur, pipeline_id, now_msc)


def _seed_envelope(dsn: str, pipeline_id: str, seq: int, epoch: int = 1) -> None:
    """A persisted envelope carrying a `seq` — what reconciliation compares the counter against."""
    envelope = {'schema_version': '2.0', 'seq': seq, 'stream_epoch': epoch,
                'pipeline_id': pipeline_id,
                'outcome_type': 'sentiment_fear_greed', 'prompt_version': '2',
                'timestamp': '2026-08-20T10:00:00Z', 'status': 'success',
                'result': [], 'metadata': {'model': 'gpt-4o-mini'}, 'errors': []}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('INSERT INTO outcomes (pipeline_id, ts, status, envelope) '
                    "VALUES (%s, %s, 'success', %s)",
                    (pipeline_id, datetime(2026, 8, 20, 10, tzinfo=timezone.utc),
                     json.dumps(envelope)))


# --- the promise ------------------------------------------------------------------------------

def test_the_first_envelope_of_a_stream_is_seq_one(clean_db, sequencer):
    """0 must stay available as 'nothing yet' — the cold-start frame uses it as a cursor origin."""
    assert _mint(clean_db, sequencer).seq == 1


def test_a_rolled_back_pass_returns_its_number(clean_db, sequencer):
    """The property the whole consumer contract rests on.

    A PostgreSQL sequence would burn the number here (`nextval` is never rolled back), leaving a
    hole that is indistinguishable from a lost record. The counter row does not: the mint lives in
    the caller's transaction, so a rollback un-does it.
    """
    assert _mint(clean_db, sequencer).seq == 1

    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        sequencer.mint(cur, 'p', _NOW)          # a pass that takes its number...
        conn.rollback()                         # ...and then fails before COMMIT

    assert _mint(clean_db, sequencer).seq == 2, 'the failed pass burned a number'


def test_each_stream_counts_on_its_own(clean_db, sequencer):
    """A fan-out variant is its own pipeline_id, so it is its own series (ISSUE_42/ISSUE_9).

    Sharing one counter across streams is exactly what disqualified `outcomes.id`: a consumer of
    one stream would see every sibling's commit as a gap.
    """
    assert [_mint(clean_db, sequencer, 'crypto_sentiment').seq for _ in range(3)] == [1, 2, 3]
    assert _mint(clean_db, sequencer, 'crypto_sentiment_4o_enhanced').seq == 1
    assert _mint(clean_db, sequencer, 'crypto_sentiment').seq == 4


def test_concurrent_passes_of_one_stream_get_distinct_ordered_numbers(clean_db, sequencer):
    """Since ISSUE_74 there is no shared pass lock, so two passes of one stream really can overlap.

    The counter's row lock is what serialises them: the slow minter holds it to COMMIT, so the fast
    one waits and no number is issued twice.
    """
    minted: list = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        minted.append(_mint(clean_db, sequencer).seq)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(minted) == [1, 2, 3, 4]


# --- the clamp --------------------------------------------------------------------------------

def test_a_backwards_clock_is_held_and_counted(clean_db, sequencer):
    """`available_msc` is the consumer's no-look-ahead gate, so it must never step back.

    A correction that moved it backwards would make a snapshot visible slightly *before* it
    actually was — small, but in the one direction a backtest must never be wrong.
    """
    first = _mint(clean_db, sequencer, now_msc=_NOW)
    assert (first.available_msc, first.resyncs) == (_NOW, 0)

    stepped_back = _mint(clean_db, sequencer, now_msc=_NOW - 4210)
    assert stepped_back.available_msc == _NOW, 'the stamp moved backwards'
    assert stepped_back.resyncs == 1
    assert stepped_back.max_correction_ms == 4210

    # A smaller later correction must not shrink the recorded maximum.
    smaller = _mint(clean_db, sequencer, now_msc=_NOW - 10)
    assert (smaller.resyncs, smaller.max_correction_ms) == (2, 4210)


def test_a_forward_clock_is_not_a_resync(clean_db, sequencer):
    first = _mint(clean_db, sequencer, now_msc=_NOW)
    later = _mint(clean_db, sequencer, now_msc=_NOW + 600_000)
    assert (later.available_msc, later.resyncs) == (_NOW + 600_000, 0)
    assert first.available_msc < later.available_msc


# --- the restore guard ------------------------------------------------------------------------

def test_a_fresh_stream_is_seeded_not_bumped(clean_db, sequencer):
    """A stream seen for the first time is not a rewind — bumping it would cry wolf on every boot."""
    assert sequencer.reconcile(['p']) == []
    assert _mint(clean_db, sequencer).epoch == 1


def test_a_consistent_series_is_left_alone(clean_db, sequencer):
    _mint(clean_db, sequencer)
    _seed_envelope(clean_db, 'p', 1)
    assert sequencer.reconcile(['p']) == []


def test_a_counter_behind_its_own_journal_bumps_the_epoch(clean_db, sequencer):
    """The detectable half of a restore: the counter was reset while the outcomes survived."""
    _mint(clean_db, sequencer)
    _seed_envelope(clean_db, 'p', 1200)         # the journal remembers further than the counter

    bumps = sequencer.reconcile(['p'])
    assert len(bumps) == 1
    assert bumps[0].reason == 'counter_behind_journal'
    assert bumps[0].previous_seq == 1 and bumps[0].new_seq == 1200
    assert bumps[0].new_epoch > bumps[0].previous_epoch

    # The series resumes past the journal, so no number is ever issued twice.
    resumed = _mint(clean_db, sequencer)
    assert resumed.seq == 1201 and resumed.epoch == bumps[0].new_epoch


def test_a_changed_cluster_bumps_the_epoch(clean_db, sequencer):
    """PITR, a promotion or a restore into a fresh cluster — none of which touches the counter."""
    _mint(clean_db, sequencer)
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE stream_seq SET cluster_id = 'other/1' WHERE pipeline_id = 'p'")

    bumps = sequencer.reconcile(['p'])
    assert len(bumps) == 1 and bumps[0].reason == 'cluster_changed'


def test_an_unrecorded_cluster_is_recorded_not_treated_as_evidence(clean_db, sequencer):
    """Absence of a fingerprint is not a changed fingerprint.

    This is what keeps a managed Postgres — where `pg_control_*` is refused and the value is None —
    from declaring a series break on every single boot.
    """
    _mint(clean_db, sequencer)
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE stream_seq SET cluster_id = NULL WHERE pipeline_id = 'p'")

    assert sequencer.reconcile(['p']) == []
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT cluster_id FROM stream_seq WHERE pipeline_id = 'p'")
        assert cur.fetchone()[0] is not None, 'the fingerprint was not recorded for next boot'


def test_the_epoch_never_repeats_even_when_its_own_value_was_rewound(clean_db, sequencer):
    """The case a plain increment cannot survive.

    A restore to a point *before* an earlier bump rewinds the epoch column itself; `+1` would then
    reissue a number a previous series already used, and two series carrying one epoch collide the
    consumer's `(pipeline_id, stream_epoch, seq)` archive key — a silent merge, which is the failure
    class this contract exists to prevent.

    The journal is what closes it: it remembers which epoch the series actually ran under, so the
    next one is chosen to exceed a *used* value rather than merely a stored one. (When the journal
    was rolled back too, only the wall-clock anchor remains — second resolution, documented in the
    sequencer as a strong anchor rather than a proof.)
    """
    _mint(clean_db, sequencer)
    _seed_envelope(clean_db, 'p', 50)
    first_bump = sequencer.reconcile(['p'])[0]

    # The engine resumes and persists under the new epoch — what the journal now remembers.
    _seed_envelope(clean_db, 'p', 51, epoch=first_bump.new_epoch)

    # A second restore rewinds the counter row past that bump, back to where it started.
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE stream_seq SET epoch = 1, seq = 10 WHERE pipeline_id = 'p'")

    second_bump = sequencer.reconcile(['p'])[0]
    assert second_bump.previous_epoch == 1, 'the setup did not reproduce a rewound epoch'
    assert second_bump.new_epoch > first_bump.new_epoch, 'an epoch was reissued'
