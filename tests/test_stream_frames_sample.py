"""The frame sample's episode selection (reissue-6) — needs a reachable Postgres.

The consumer asked for a sample that shows `breaking_episode_id` **populated**, and named the case
their reader is most likely to get wrong: a pass where `is_breaking` is false while the id persists.
The generator therefore no longer takes "the last two passes" — it looks for an episode carrying all
three shapes.

That selection cannot be checked against the dev journal (it holds no stamped envelopes), and it
runs on the server against production data. So it is checked here, against envelopes written for the
purpose: a sample that quietly showed two ordinary passes would answer the consumer's question with
the shape they already had.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'experiments' / 'stream_frames_sample'))
generate = pytest.importorskip('generate')

_T0 = datetime(2026, 8, 24, 16, 51, 3, tzinfo=timezone.utc)
_EPISODE = 'forex_macro_sentiment:usd cad boc:2026-08-24T16:51:03Z'


def _envelope(seq: int, minutes: int, *, is_breaking: bool, start: bool,
              episode_id: str = _EPISODE, status: str = 'success') -> Dict[str, Any]:
    ts = _T0 + timedelta(minutes=minutes)
    return {
        'schema_version': '2.0', 'seq': seq, 'stream_epoch': 1,
        'pipeline_id': 'forex_macro_sentiment', 'outcome_type': 'sentiment_fear_greed',
        'prompt_version': '3', 'timestamp': ts.isoformat().replace('+00:00', 'Z'),
        'status': status, 'metadata': {'model': 'gpt-4o-mini'}, 'errors': [],
        'result': [{'symbol': 'USDCAD', 'signal': 'SELL', 'sentiment_score': -0.6,
                    'confidence': 0.8, 'reasoning': 'BOC review', 'urgency': 0.9 if is_breaking else 0.7,
                    'is_breaking': is_breaking, 'basis': 'llm',
                    'breaking_episode_id': episode_id, 'breaking_episode_start': start}],
    }


def _store(dsn: str, envelopes: List[Dict[str, Any]]) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for envelope in envelopes:
            cur.execute(
                'INSERT INTO outcomes (pipeline_id, ts, status, envelope) VALUES (%s, %s, %s, %s)',
                (envelope['pipeline_id'], envelope['timestamp'], envelope['status'],
                 json.dumps(envelope)))
        conn.commit()


def test_the_trio_is_opener_continuation_and_hold_band(clean_db: str) -> None:
    """The three shapes, in that order — and the hold-band pass is the one that matters."""
    _store(clean_db, [
        _envelope(1, 0, is_breaking=True, start=True),        # opener
        _envelope(2, 10, is_breaking=True, start=False),      # continuation
        _envelope(3, 20, is_breaking=False, start=False),     # hold band: id persists, flag does not
    ])

    trio = generate._fetch_episode_trio(clean_db)

    assert len(trio) == 3
    opener, continuation, hold = (env['result'][0] for env in trio)
    assert opener['breaking_episode_start'] is True
    assert continuation['breaking_episode_start'] is False and continuation['is_breaking'] is True
    assert hold['is_breaking'] is False
    # The point of the whole sample: one id across all three.
    assert {row['breaking_episode_id'] for row in (opener, continuation, hold)} == {_EPISODE}


def test_an_episode_without_a_hold_band_pass_is_not_chosen(clean_db: str) -> None:
    """An episode that never dipped cannot demonstrate the case the consumer asked about, so it is
    skipped in favour of one that can — and if none can, the generator refuses rather than shipping
    a sample that answers the wrong question."""
    _store(clean_db, [
        _envelope(1, 0, is_breaking=True, start=True, episode_id='never-dipped'),
        _envelope(2, 10, is_breaking=True, start=False, episode_id='never-dipped'),
    ])

    with pytest.raises(SystemExit, match='no episode carries all three shapes'):
        generate._fetch_episode_trio(clean_db)


def test_a_partial_pass_is_never_used(clean_db: str) -> None:
    """`status: partial` is excluded by design — a degraded pass is not a contract sample.

    This is why the crypto stream could not be used while one of its feeds was quarantined.
    """
    _store(clean_db, [
        _envelope(1, 0, is_breaking=True, start=True),
        _envelope(2, 10, is_breaking=True, start=False),
        _envelope(3, 20, is_breaking=False, start=False, status='partial'),
    ])

    with pytest.raises(SystemExit, match='no episode carries all three shapes'):
        generate._fetch_episode_trio(clean_db)
