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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'experiments' / 'stream_frames_sample'))
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

    # `_fetch_episode_trio` returns the selected episode id alongside its three passes: `build` has
    # to verify the passes belong to the episode the query picked, and an envelope carries every
    # symbol of its pipeline, so several ids legitimately appear in one pass.
    assert trio.episode_id == _EPISODE
    assert len(trio.envelopes) == 3
    opener, continuation, hold = (env['result'][0] for env in trio.envelopes)
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


def _row(symbol: str, episode_id: str, *, start: bool, is_breaking: bool) -> Dict[str, Any]:
    return {'symbol': symbol, 'signal': 'SELL', 'sentiment_score': -0.4, 'confidence': 0.8,
            'reasoning': 'r', 'urgency': 0.8 if is_breaking else 0.6, 'is_breaking': is_breaking,
            'basis': 'llm', 'breaking_episode_id': episode_id, 'breaking_episode_start': start}


def test_a_second_concurrent_episode_does_not_break_the_check() -> None:
    """Two symbols inside their own episodes at once is normal — and it once refused a valid sample.

    The check used to assert "exactly one episode id across all rows". An envelope carries every
    symbol of its pipeline, so that only ever held while a single symbol was in an episode. On
    2026-08-26 the generator selected a USDJPY episode carrying all three shapes, built the sample,
    and then refused it because USDCAD's own episode was still open in the same envelopes. Both ids
    were correct; the assertion was not. No database needed — this is the pure check.
    """
    other = 'forex_macro_sentiment:usd jpy boj:2026-08-26T00:30:20Z'
    trio = tuple(
        {'result': [_row('USDCAD', _EPISODE, start=start, is_breaking=is_breaking),
                    _row('USDJPY', other, start=False, is_breaking=True)]}
        for start, is_breaking in ((True, True), (False, True), (False, False)))

    generate._check_one_episode(_EPISODE, trio)     # the sampled episode passes

    # And the check still bites. The concurrent episode is present in all three passes but does not
    # open in the first one, so it fails on the earliest shape rule rather than being waved through.
    with pytest.raises(AssertionError, match='not the opener'):
        generate._check_one_episode(other, trio)

    # A trio that genuinely spans two episodes is still refused.
    broken = (trio[0], trio[1], {'result': [_row('USDCAD', other, start=False, is_breaking=False)]})
    with pytest.raises(AssertionError, match='absent from frame'):
        generate._check_one_episode(_EPISODE, broken)


# --- the selection must not reach back past ISSUE_65 (found on the PRODUCTION journal) -----------

def _with_legacy_row(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Append a row from before ISSUE_65 — the two episode keys are ABSENT, not null.

    This is the production shape exactly: an envelope whose in-episode row was stamped by #108's
    backfill while a row outside the episode was left untouched, so the keys never appeared on it.
    """
    envelope['result'].append({
        'symbol': 'EURUSD', 'signal': 'HOLD', 'sentiment_score': 0.0, 'confidence': 0.5,
        'reasoning': 'no strong signal', 'urgency': 0.2, 'is_breaking': False, 'basis': 'llm'})
    return envelope


def _trio_for(episode_id: str, first_seq: int, offset: int, *, legacy: bool = False):
    """The three shapes of one episode, optionally with a legacy row on the opener."""
    opener = _envelope(first_seq, offset, is_breaking=True, start=True, episode_id=episode_id)
    return [
        _with_legacy_row(opener) if legacy else opener,
        _envelope(first_seq + 1, offset + 10, is_breaking=True, start=False, episode_id=episode_id),
        _envelope(first_seq + 2, offset + 20, is_breaking=False, start=False, episode_id=episode_id),
    ]


def test_an_episode_with_a_legacy_row_is_skipped_for_a_fully_native_one(clean_db: str) -> None:
    """A pre-ISSUE_65 row has no `breaking_episode_id` KEY, so its envelope cannot be evidence.

    Found by running the generator against production: the newest qualifying episode contained a row
    from before the field existed, the contract check refused it — and the fill-in meant to cover
    exactly that case could never run, because it sat behind the check that forbids it. The generator
    promised an injection its own guard made impossible.

    The fix belongs in the SELECTION, not in the check: only envelopes whose every row carries the
    identity natively may be chosen. Pinned by offering the query a legacy episode that is NEWER than
    a native one, because recency alone would pick the wrong one.
    """
    _store(clean_db, _trio_for('native-episode', 1, 0)
           + _trio_for('legacy-episode', 10, 120, legacy=True))

    trio = generate._fetch_episode_trio(clean_db)

    assert trio.episode_id == 'native-episode'
    assert len(trio.envelopes) == 3
    for envelope in trio.envelopes:
        for row in envelope['result']:
            assert 'breaking_episode_id' in row        # native on every row, never filled in
            assert 'breaking_episode_start' in row


def test_the_refusal_names_the_legacy_cause_rather_than_the_missing_shape(clean_db: str) -> None:
    """The refusal a reader first met was ambiguous between two causes and cost a round trip.

    "No episode carries all three shapes" is the wrong message when an episode *does* carry them and
    was rejected for a legacy row: the reader then hunts for a hold-band pass that is actually there.
    """
    _store(clean_db, _trio_for('legacy-only', 1, 0, legacy=True))

    with pytest.raises(SystemExit) as excinfo:
        generate._fetch_episode_trio(clean_db)

    message = str(excinfo.value)
    assert 'legacy-only' in message                    # names the episode it rejected
    assert 'ISSUE_65' in message and 'native' in message
    assert 'all three shapes' in message               # and says they WERE present


def test_a_role_pass_carrying_a_legacy_row_is_not_chosen(clean_db: str) -> None:
    """The predicate belongs on both queries. An episode can qualify on its native passes while a
    DIFFERENT pass of it carries a legacy row — picking that one for a role would reintroduce
    precisely what the selection excluded."""
    envelopes = _trio_for('mixed-episode', 1, 0)
    # A fourth pass of the same episode, newer and hold-band shaped, but with a legacy row.
    envelopes.append(_with_legacy_row(
        _envelope(4, 30, is_breaking=False, start=False, episode_id='mixed-episode')))
    _store(clean_db, envelopes)

    trio = generate._fetch_episode_trio(clean_db)

    assert trio.episode_id == 'mixed-episode'
    hold_band = trio.envelopes[2]
    assert [row['symbol'] for row in hold_band['result']] == ['USDCAD']   # the native pass, seq 3
