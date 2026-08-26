"""Episode identity backfill (ISSUE_108) — the replay, the self-check, and both sinks.

Against a real database, because everything worth checking here is a write: `jsonb_set` on a key
that may not exist yet, an idempotent second run, and a registry row that must not double-count a
fanned pair. The replay itself is the LIVE tracker (`BreakingEpisodeTracker`), so the first test is
parity against it — that agreement is the whole argument for driving the tracker instead of a walk
lifted out of the funnel report.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg
import pytest

from finiexragengine.core.outcome.episode_backfill import (
    EpisodeBackfill,
    format_backfill_plan,
)
from finiexragengine.core.pipeline.breaking_episode import BreakingEpisodeTracker
from finiexragengine.core.pipeline.breaking_episode_rule import (
    BreakingEpisodeRule,
    EpisodeGrouping,
)
from finiexragengine.types.outcome_types import SentimentEnvelope

_T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
_PIPELINE = 'crypto_sentiment'


def _grouping(**kwargs: Any) -> Dict[str, EpisodeGrouping]:
    """One pipeline's grouping. `gap` defaults to production's 150 minutes."""
    query_map = kwargs.pop('query_map', {})
    return {_PIPELINE: EpisodeGrouping(
        BreakingEpisodeRule(exit_threshold=kwargs.pop('exit_threshold', 0.7),
                            gap=kwargs.pop('gap', timedelta(minutes=150))),
        query_map=query_map)}


def _envelope(ts: datetime, results: List[Dict[str, Any]], *, status: str = 'success',
              carried: bool = False) -> Dict[str, Any]:
    """One stored envelope, built through the model so it is genuinely valid.

    `carried=False` **deletes** the two episode keys rather than storing them as null: an envelope
    produced before ISSUE_65 has no such key at all, which is the case `jsonb_set(..., create_missing
    => true)` exists for. Storing null would quietly test the easier path.
    """
    envelope = SentimentEnvelope(
        pipeline_id=_PIPELINE, outcome_type='sentiment', prompt_version='2',
        timestamp=ts, status=status,
        metadata={'model': 'gpt-4o-mini'},
        result=[{'symbol': r['symbol'], 'signal': r.get('signal', 'BUY'),
                 'sentiment_score': 0.5, 'confidence': 0.8, 'reasoning': r.get('reasoning', 'why'),
                 'urgency': r['urgency'], 'is_breaking': r['urgency'] >= 0.8,
                 'basis': 'llm', 'sources': [],
                 'base_currency': r.get('base_currency'),
                 'breaking_episode_id': r.get('episode_id'),
                 'breaking_episode_start': r.get('episode_start', False)}
                for r in results])
    raw = envelope.model_dump(mode='json')
    if not carried:
        for row in raw['result']:
            row.pop('breaking_episode_id', None)
            row.pop('breaking_episode_start', None)
    return raw


def _store(dsn: str, envelopes: List[Dict[str, Any]]) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for envelope in envelopes:
            cur.execute('INSERT INTO outcomes (pipeline_id, ts, status, envelope) '
                        'VALUES (%s, %s, %s, %s)',
                        (envelope['pipeline_id'], envelope['timestamp'], envelope['status'],
                         json.dumps(envelope)))
        conn.commit()


def _stored(dsn: str) -> List[Dict[str, Any]]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('SELECT envelope FROM outcomes ORDER BY ts, id')
        return [row[0] for row in cur.fetchall()]


def _ids(dsn: str) -> List[List[Optional[str]]]:
    return [[r.get('breaking_episode_id') for r in env['result']] for env in _stored(dsn)]


def _episode_rows(dsn: str) -> List[Dict[str, Any]]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('SELECT episode_id, episode_key, symbol, n_passes, started_at '
                    'FROM breaking_episodes ORDER BY episode_id')
        return [{'episode_id': r[0], 'episode_key': r[1], 'symbol': r[2], 'n_passes': r[3],
                 'started_at': r[4]} for r in cur.fetchall()]


def _series(urgencies: List[float], *, symbol: str = 'BTCUSD',
            start: datetime = _T0, step_minutes: int = 10) -> List[Dict[str, Any]]:
    return [_envelope(start + timedelta(minutes=step_minutes * i), [{'symbol': symbol, 'urgency': u}])
            for i, u in enumerate(urgencies)]


def _backfill(dsn: str, **kwargs: Any) -> EpisodeBackfill:
    return EpisodeBackfill(dsn, kwargs.pop('groupings', _grouping()),
                           prologue=kwargs.pop('prologue', timedelta(hours=72)))


# --- the argument for driving the tracker -------------------------------------------------------

def test_the_backfilled_ids_are_what_the_live_tracker_produces(clean_db: str) -> None:
    """Parity with the LIVE path, not with the funnel report.

    This is the property the DoD deviation buys: the same function that minted every served id
    mints the backfilled ones, so the two cannot drift. Driven here twice over one series — once
    through the tracker directly (as an eval worker does) and once through the backfill.
    """
    urgencies = [0.8, 0.7, 0.9, 0.2, 0.8]
    _store(clean_db, _series(urgencies))

    live = BreakingEpisodeTracker(_grouping()[_PIPELINE])
    expected = []
    for raw in _series(urgencies):
        envelope = SentimentEnvelope.model_validate(raw)
        live.observe(envelope)
        expected.append([r.breaking_episode_id for r in envelope.result])

    _backfill(clean_db).apply(_T0 - timedelta(days=1), _T0 + timedelta(days=1))
    assert _ids(clean_db) == expected
    assert any(row[0] for row in expected)          # the fixture really did open an episode


# --- the self-check ----------------------------------------------------------------------------

def test_a_served_id_is_compared_and_never_overwritten(clean_db: str) -> None:
    """Where the archive already carries an identity, the replay must reproduce it."""
    _store(clean_db, [_envelope(_T0, [{'symbol': 'BTCUSD', 'urgency': 0.8,
                                       'episode_id': f'{_PIPELINE}:BTCUSD:2026-07-20T12:00:00Z',
                                       'episode_start': True}], carried=True)])
    plan = _backfill(clean_db).apply(_T0 - timedelta(days=1), _T0 + timedelta(days=1))

    assert plan.carried == 1 and plan.would_stamp == 0
    assert plan.disagreements == []
    assert _ids(clean_db) == [[f'{_PIPELINE}:BTCUSD:2026-07-20T12:00:00Z']]


def test_a_planted_disagreement_aborts_the_write(clean_db: str) -> None:
    """The served id is wrong on purpose; nothing may be written and the run must say why."""
    _store(clean_db, [_envelope(_T0, [{'symbol': 'BTCUSD', 'urgency': 0.8,
                                       'episode_id': 'crypto_sentiment:BTCUSD:1999-01-01T00:00:00Z',
                                       'episode_start': True}], carried=True),
                      _envelope(_T0 + timedelta(hours=6), [{'symbol': 'ETHUSD', 'urgency': 0.8}])])
    plan = _backfill(clean_db).apply(_T0 - timedelta(days=1), _T0 + timedelta(days=1))

    assert len(plan.disagreements) == 1
    disagreement = plan.disagreements[0]
    assert disagreement.served_id.endswith('1999-01-01T00:00:00Z')
    assert disagreement.computed_id.endswith('2026-07-20T12:00:00Z')
    assert not plan.applied and plan.envelopes_written == 0
    # The ETHUSD row was stampable and must ALSO be untouched — an abort is not partial.
    assert _ids(clean_db) == [['crypto_sentiment:BTCUSD:1999-01-01T00:00:00Z'], [None]]
    assert _episode_rows(clean_db) == []
    rendered = format_backfill_plan(plan)
    assert 'nothing was written' in rendered and '--apply refuses' in rendered


# --- writes -------------------------------------------------------------------------------------

def test_a_dry_run_writes_nothing(clean_db: str) -> None:
    """Asserted against the tables, not against the wording of the output."""
    _store(clean_db, _series([0.8, 0.8]))
    plan = _backfill(clean_db).plan(_T0 - timedelta(days=1), _T0 + timedelta(days=1))

    assert plan.would_stamp == 2 and not plan.applied
    assert _ids(clean_db) == [[None], [None]]
    assert _episode_rows(clean_db) == []
    assert 're-run with --apply' in format_backfill_plan(plan)


def test_only_the_two_episode_keys_change(clean_db: str) -> None:
    """`jsonb_set` on two paths, so 'nothing else can move' is provable rather than argued."""
    _store(clean_db, _series([0.8]))
    before = _stored(clean_db)[0]
    _backfill(clean_db).apply(_T0 - timedelta(days=1), _T0 + timedelta(days=1))
    after = _stored(clean_db)[0]

    def stripped(envelope: Dict[str, Any]) -> Dict[str, Any]:
        copy = json.loads(json.dumps(envelope))
        for row in copy['result']:
            row.pop('breaking_episode_id', None)
            row.pop('breaking_episode_start', None)
        return copy

    assert stripped(before) == stripped(after)
    assert after['result'][0]['breaking_episode_id']       # and the two keys did arrive
    assert after['result'][0]['breaking_episode_start'] is True


def test_a_second_apply_changes_nothing(clean_db: str) -> None:
    """Idempotent: the second run finds every id carried, and its self-check validates the first.

    Note what re-running proves beyond idempotency — the ids written by run one are compared
    against a fresh replay in run two, so a corrupt write would surface as a disagreement rather
    than as silence.
    """
    _store(clean_db, _series([0.8, 0.7, 0.8]))
    backfill = _backfill(clean_db)
    first = backfill.apply(_T0 - timedelta(days=1), _T0 + timedelta(days=1))
    after_first = (_ids(clean_db), _episode_rows(clean_db))

    second = backfill.apply(_T0 - timedelta(days=1), _T0 + timedelta(days=1))

    assert first.would_stamp == 3 and second.would_stamp == 0
    assert second.carried == 3 and second.disagreements == []
    assert (_ids(clean_db), _episode_rows(clean_db)) == after_first


def test_both_sinks_agree(clean_db: str) -> None:
    _store(clean_db, _series([0.8, 0.7, 0.9]))
    _backfill(clean_db).apply(_T0 - timedelta(days=1), _T0 + timedelta(days=1))

    stamped = {value for row in _ids(clean_db) for value in row if value}
    registered = {row['episode_id'] for row in _episode_rows(clean_db)}
    assert stamped and stamped == registered


# --- the finding the issue does not mention -----------------------------------------------------

def test_the_prologue_keeps_an_episode_opened_before_the_range_on_its_real_start(
        clean_db: str) -> None:
    """Without it the first in-range pass mints an id from a CLIPPED start — a second identity
    for a story already running, which is what the tracker's adopt-branch prevents at boot.

    The episode opens at 11:00 carrying its served id, and `--since` is 12:00. With a prologue the
    continuation keeps the 11:00 anchor; with none, it invents an episode at 12:00.
    """
    opened_at = _T0 - timedelta(hours=1)
    served = f'{_PIPELINE}:BTCUSD:{opened_at:%Y-%m-%dT%H:%M:%S}Z'
    _store(clean_db, [
        _envelope(opened_at, [{'symbol': 'BTCUSD', 'urgency': 0.8, 'episode_id': served,
                               'episode_start': True}], carried=True),
        _envelope(_T0, [{'symbol': 'BTCUSD', 'urgency': 0.8}]),
    ])

    with_prologue = _backfill(clean_db, prologue=timedelta(hours=72))
    with_prologue.apply(_T0, _T0 + timedelta(days=1))
    assert _ids(clean_db)[1] == [served]                     # the real start survives

    # Same fixture, no prologue: the range starts cold and the continuation opens its own episode.
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute('TRUNCATE outcomes, breaking_episodes')
        conn.commit()
    _store(clean_db, [
        _envelope(opened_at, [{'symbol': 'BTCUSD', 'urgency': 0.8, 'episode_id': served,
                               'episode_start': True}], carried=True),
        _envelope(_T0, [{'symbol': 'BTCUSD', 'urgency': 0.8}]),
    ])
    _backfill(clean_db, prologue=timedelta(0)).apply(_T0, _T0 + timedelta(days=1))
    clipped = _ids(clean_db)[1][0]
    assert clipped is not None and clipped != served
    assert clipped.endswith(f'{_T0:%Y-%m-%dT%H:%M:%S}Z')     # anchored on the clipped start


# --- the fanned pair ---------------------------------------------------------------------------

def test_a_fanned_pair_is_one_episode_and_one_registry_row_per_pass(clean_db: str) -> None:
    """ISSUE_70: ETHUSD/ETHEUR share a retrieval query, so they share an episode.

    Both legs get the id, and `n_passes` counts passes rather than symbols — the dedupe the tracker
    does per episode id, carried through here rather than re-derived.
    """
    groupings = _grouping(query_map={'ETHUSD': 'Ethereum ETH', 'ETHEUR': 'Ethereum ETH'})
    _store(clean_db, [
        _envelope(_T0, [{'symbol': 'ETHUSD', 'urgency': 0.8, 'base_currency': 'ETH'},
                        {'symbol': 'ETHEUR', 'urgency': 0.8, 'base_currency': 'ETH'}]),
        _envelope(_T0 + timedelta(minutes=10),
                  [{'symbol': 'ETHUSD', 'urgency': 0.7, 'base_currency': 'ETH'},
                   {'symbol': 'ETHEUR', 'urgency': 0.7, 'base_currency': 'ETH'}]),
    ])
    _backfill(clean_db, groupings=groupings).apply(_T0 - timedelta(days=1),
                                                   _T0 + timedelta(days=1))

    first, second = _ids(clean_db)
    assert first[0] == first[1] and second[0] == second[1] == first[0]
    rows = _episode_rows(clean_db)
    assert len(rows) == 1
    assert rows[0]['episode_key'] == 'Ethereum ETH'
    assert rows[0]['n_passes'] == 2                  # two passes, not four results


# --- degenerate ---------------------------------------------------------------------------------

def test_an_error_envelope_is_outside_the_replay(clean_db: str) -> None:
    """Same filter the store reports use, so the rule sees the same population."""
    _store(clean_db, [_envelope(_T0, [{'symbol': 'BTCUSD', 'urgency': 0.8}], status='error')])
    plan = _backfill(clean_db).plan(_T0 - timedelta(days=1), _T0 + timedelta(days=1))
    assert plan.pipelines == [] and plan.would_stamp == 0


def test_an_empty_range_renders_rather_than_raising(clean_db: str) -> None:
    plan = _backfill(clean_db).plan(_T0, _T0 + timedelta(days=1))
    assert '(no envelopes in the range)' in format_backfill_plan(plan)


def test_a_dry_run_before_an_apply_does_not_change_what_the_apply_writes(clean_db: str) -> None:
    """The sequence a human actually performs, and the one that found the state bug.

    `BreakingEpisodeRule` holds the open-episode state and lives on the grouping, so a second
    replay on the same instance used to inherit the first one's episodes: the opening pass read as
    a continuation and `breaking_episode_start` came out false while the id stayed right. A
    half-populated field is worse than an absent one — the consumer derives their `opened` edge
    from exactly that flag.
    """
    _store(clean_db, _series([0.8, 0.7, 0.8]))
    backfill = _backfill(clean_db)
    window = (_T0 - timedelta(days=1), _T0 + timedelta(days=1))

    preview = backfill.plan(*window)
    applied = backfill.apply(*window)

    assert preview.would_stamp == applied.would_stamp == 3
    assert applied.disagreements == []
    starts = [row['result'][0]['breaking_episode_start'] for row in _stored(clean_db)]
    assert starts == [True, False, False]            # exactly one opener, and it is the first pass
