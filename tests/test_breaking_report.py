"""Breaking report aggregation (ISSUE_11) — reaction math + episode grouping, DB-free.

Tests `_aggregate` directly with synthetic store rows (envelope dicts), so no DB is needed.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.breaking_report import (
    _aggregate,
    format_breaking_report,
)

_T0 = datetime(2026, 7, 13, 14, 0, 5, tzinfo=timezone.utc)
_T1 = datetime(2026, 7, 13, 14, 0, 12, tzinfo=timezone.utc)
_T3 = datetime(2026, 7, 13, 14, 0, 54, tzinfo=timezone.utc)


def _row(pipeline, t3, *, symbol='BTCUSD', is_breaking=True, published=None, fetched=None,
         signal='SELL', reason=''):
    source = {}
    if published is not None:
        source['published_at'] = published.isoformat()
    if fetched is not None:
        source['fetched_at'] = fetched.isoformat()
    return (pipeline, {
        'timestamp': t3.isoformat(),
        'result': [{'symbol': symbol, 'is_breaking': is_breaking, 'signal': signal,
                    'reasoning': reason, 'sources': [source] if source else []}],
    })


def test_reaction_math_engine_vs_end_to_end():
    report = _aggregate([_row('crypto_sentiment', _T3, published=_T0, fetched=_T1)],
                        flagged=3, since_label='7d')
    assert report.confirmed_episodes == 1 and report.flagged_candidates == 3
    row = report.rows[0]
    assert row.engine_reaction_s == [42.0]      # t3 − earliest fetched_at (54 − 12)
    assert row.end_to_end_s == [49.0]           # t3 − earliest published_at (54 − 5)


def test_consecutive_breakings_are_one_episode():
    base = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row('p', base, fetched=base - timedelta(seconds=30)),
        _row('p', base + timedelta(minutes=5), fetched=base),   # same story, within the gap
    ]
    report = _aggregate(rows, 0, '7d')
    assert report.confirmed_episodes == 1                       # one episode, not two
    assert report.rows[0].engine_reaction_s == [30.0]          # sampled on the FIRST only


def test_re_break_after_the_gap_is_a_new_episode():
    base = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    rows = [_row('p', base), _row('p', base + timedelta(minutes=45))]   # > 30min apart
    assert _aggregate(rows, 0, '7d').confirmed_episodes == 2


def test_non_breaking_rows_are_ignored():
    report = _aggregate([_row('p', _T3, is_breaking=False)], flagged=5, since_label='7d')
    assert report.confirmed_episodes == 0 and report.rows == []


def test_missing_fetched_at_still_reports_end_to_end():
    # A pre-ISSUE_11 envelope has no fetched_at → engine-reaction unavailable, e2e still works.
    report = _aggregate([_row('p', _T3, published=_T0)], 0, '7d')
    row = report.rows[0]
    assert row.engine_reaction_s == [] and row.end_to_end_s == [49.0]


def test_format_renders_windows_and_funnel():
    report = _aggregate([_row('crypto_sentiment', _T3, published=_T0, fetched=_T1)], 3, '7d')
    out = format_breaking_report(report)
    assert 'Breaking Detection' in out
    assert 'window: last 7d' in out
    assert '3 flagged → 1 confirmed' in out


def test_episode_listing_shows_started_duration_and_reason():
    # ISSUE_64: the per-episode listing groups by pipeline, one line each — started, duration, why.
    base = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row('crypto_sentiment', base, symbol='ETHUSD', signal='SELL',
             reason='greed rising — Musk confirms ETH buy-in', fetched=base - timedelta(seconds=30)),
        _row('crypto_sentiment', base + timedelta(minutes=5), symbol='ETHUSD', signal='SELL',
             reason='still hot', fetched=base),                     # same episode → extends duration
    ]
    report = _aggregate(rows, 0, '7d')
    assert len(report.episodes) == 1                                # one edge-triggered episode
    episode = report.episodes[0]
    assert episode.symbol == 'ETHUSD' and episode.signal == 'SELL'
    assert episode.duration_s == 300.0                              # 5-min span (last − start)
    assert episode.reason == 'greed rising — Musk confirms ETH buy-in'   # frozen at the start
    out = format_breaking_report(report)
    assert 'Breaking episodes — last 7d' in out
    assert 'ETHUSD' in out and 'Musk confirms ETH buy-in' in out
    assert '5.0m' in out                                            # duration rendered
