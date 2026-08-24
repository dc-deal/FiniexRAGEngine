"""The persisted episode registry (ISSUE_65) — needs a reachable Postgres (skipped otherwise).

Runs against the canonical `breaking_episodes` table inside the migration-built test schema
(`clean_db`), so a migration that drifts from the code fails here instead of hiding behind test DDL
— the same arrangement `test_stream_sequencer.py` uses.

The unit's promise is narrow and worth stating: an episode's identity row is created once, advanced
by every later pass of that episode, and never rewritten by one. The descriptive fields describe the
EDGE; only `last_seen_at` and `n_passes` move.
"""
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import psycopg
import pytest

from finiexragengine.core.outcome.episode_registry import EpisodeRegistry
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.types.eval_types import EpisodeUpsert
from finiexragengine.types.outcome_types import AnalysisEnvelope, RunMetadata, SentimentResult

_T0 = datetime(2026, 8, 24, 16, 51, 3, tzinfo=timezone.utc)
_EPISODE = 'crypto_sentiment:eth news:2026-08-24T16:51:03Z'


def _row(opened: bool, minutes: int = 0, **overrides: Any) -> EpisodeUpsert:
    row = EpisodeUpsert(
        episode_id=_EPISODE, pipeline_id='crypto_sentiment', episode_key='eth news',
        symbol='ETHUSD', signal='SELL', started_at=_T0,
        last_seen_at=_T0 + timedelta(minutes=minutes), opened=opened, urgency=0.9,
        reason='ECB signals an emergency review', breaking_reason='ECB emergency review announced',
        prompt_version='3', engine_s=42.0 if opened else None,
        end_to_end_s=91.0 if opened else None)
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _write(dsn: str, *rows: EpisodeUpsert) -> None:
    """Each row in its own committed transaction — the shape `OutcomeStore.save` uses."""
    registry = EpisodeRegistry()
    for row in rows:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            registry.upsert(cur, row)


def _read(dsn: str) -> Optional[Tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('SELECT n_passes, last_seen_at, signal, urgency, engine_s, reason '
                    'FROM breaking_episodes WHERE episode_id = %s', (_EPISODE,))
        return cur.fetchone()


def test_the_opening_pass_creates_the_row(clean_db: str) -> None:
    _write(clean_db, _row(opened=True))
    n_passes, last_seen, signal, urgency, engine_s, _ = _read(clean_db)

    assert (n_passes, signal, urgency, engine_s) == (1, 'SELL', 0.9, 42.0)
    assert last_seen == _T0


def test_a_continuation_advances_the_row_without_rewriting_it(clean_db: str) -> None:
    """`last_seen_at` and `n_passes` move; the edge's own measurements stay put.

    Re-sampling a reaction time against ageing evidence is the defect ISSUE_81 removed from this
    metric — a continuation that overwrote `engine_s` would put it straight back.
    """
    _write(clean_db, _row(opened=True))
    _write(clean_db, _row(opened=False, minutes=20, signal='BUY', urgency=0.7,
                          reason='a later pass says something else'))
    n_passes, last_seen, signal, urgency, engine_s, reason = _read(clean_db)

    assert n_passes == 2
    assert last_seen == _T0 + timedelta(minutes=20)
    assert (signal, urgency, engine_s) == ('SELL', 0.9, 42.0)   # frozen at the opening pass
    assert reason == 'ECB signals an emergency review'


def test_replaying_the_same_pass_does_not_inflate_the_count(clean_db: str) -> None:
    """A retried transaction must not show up as an extra pass in the episode's history."""
    _write(clean_db, _row(opened=True), _row(opened=False), _row(opened=False))
    n_passes, last_seen, *_ = _read(clean_db)

    assert n_passes == 1
    assert last_seen == _T0


def test_an_out_of_order_pass_never_moves_last_seen_backwards(clean_db: str) -> None:
    _write(clean_db, _row(opened=True), _row(opened=False, minutes=20),
           _row(opened=False, minutes=10))
    n_passes, last_seen, *_ = _read(clean_db)

    assert last_seen == _T0 + timedelta(minutes=20)
    assert n_passes == 2   # the late arrival advanced nothing, so it counted nothing


def test_concurrent_writers_produce_one_row_not_a_conflict(clean_db: str) -> None:
    """Since ISSUE_74 removed the shared pass lock, two eval workers write at genuinely the same
    moment (the ISSUE_42 model variants score the same symbols). A read-check-insert would give
    either a duplicate or a constraint error here; the upsert gives one row."""
    errors: list = []

    def writer(minutes: int) -> None:
        try:
            _write(clean_db, _row(opened=True, minutes=minutes))
        except Exception as exc:   # noqa: BLE001 — the assertion is that none of these happen
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(minutes,)) for minutes in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM breaking_episodes WHERE episode_id = %s', (_EPISODE,))
        assert cur.fetchone()[0] == 1


def test_the_envelope_and_its_episode_commit_together(clean_db: str) -> None:
    """The reason the registry takes a cursor instead of opening its own connection: a journal row
    referencing an episode the registry never received would be a dangling identity."""
    store = OutcomeStore(clean_db)
    envelope = AnalysisEnvelope(
        pipeline_id='crypto_sentiment', outcome_type='sentiment_fear_greed', prompt_version='3',
        timestamp=_T0, status='success', metadata=RunMetadata(model='gpt-4o-mini'),
        result=[SentimentResult(symbol='ETHUSD', signal='SELL', sentiment_score=-0.6,
                                confidence=0.8, reasoning='ECB signals an emergency review',
                                urgency=0.9, is_breaking=True, breaking_episode_id=_EPISODE,
                                breaking_episode_start=True)])
    store.save(envelope, None, [_row(opened=True)])

    stored = store.get_latest('crypto_sentiment')
    assert stored is not None
    assert stored.result[0].breaking_episode_id == _EPISODE      # reached the JSONB
    assert stored.result[0].breaking_episode_start is True
    assert _read(clean_db)[0] == 1                               # and the registry row exists
