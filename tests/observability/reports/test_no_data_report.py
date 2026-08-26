"""No-data / retrieval-coverage report (ISSUE_27).

Tests `_aggregate` directly with synthetic store rows (envelope dicts), so no DB is needed.
"""
from finiexragengine.core.observability.reports.no_data_report import (
    _aggregate,
    format_no_data_report,
)


def _row(pipeline, symbol, *, basis='llm', best_distance=None, floor=None, kept=None):
    """One store row: (pipeline_id, envelope) with a single-symbol result + funnel."""
    funnel = {}
    if best_distance is not None:
        funnel['best_distance'] = best_distance
    if floor is not None:
        funnel['floor'] = floor
    if kept is not None:
        funnel['kept'] = kept
    return (pipeline, {
        'result': [{'symbol': symbol, 'basis': basis}],
        'metadata': {'per_symbol_retrieval': {symbol: funnel} if funnel else {}},
    })


def test_delivering_symbol_makes_no_row():
    report = _aggregate([_row('p', 'BTCUSD', kept=3, floor=0.7)], '7d')
    assert report.all_delivering
    assert report.symbols_seen == 1


def test_silent_symbol_aggregates_share_and_nearest_miss():
    rows = [
        _row('p', 'ETHUSD', basis='no_data', best_distance=0.72, floor=0.70),
        _row('p', 'ETHUSD', basis='no_data', best_distance=0.68, floor=0.70),
        _row('p', 'ETHUSD', kept=2, floor=0.70),
    ]
    report = _aggregate(rows, '7d')
    row = report.rows[0]
    assert (row.passes, row.no_data_passes) == (3, 2)
    assert row.share == 2 / 3
    assert row.nearest_miss_min == 0.68
    assert row.nearest_miss_avg == 0.70
    assert row.kept_avg == 2.0            # averaged over delivering passes only


def test_candidate_needs_share_and_margin():
    # 100% silent, nearest miss 0.681 vs floor 0.70 → within 0.02 above? 0.681 < 0.70,
    # miss - floor is negative (article was *inside* the floor on an earlier snapshot) —
    # still a candidate: the margin is an upper bound, not a band.
    hot = _aggregate([_row('p', 'ETHUSD', basis='no_data', best_distance=0.705, floor=0.70)],
                     '7d').rows[0]
    assert hot.candidate
    # Nearest miss far above the floor → genuinely no news, not a calibration problem.
    cold = _aggregate([_row('p', 'DASHUSD', basis='no_data', best_distance=0.74, floor=0.70)],
                      '7d').rows[0]
    assert not cold.candidate
    # Mostly delivering (share < 0.5) → not a candidate even with a close miss.
    rows = [_row('p', 'BTCUSD', basis='no_data', best_distance=0.705, floor=0.70),
            _row('p', 'BTCUSD', kept=2), _row('p', 'BTCUSD', kept=1)]
    assert not _aggregate(rows, '7d').rows[0].candidate


def test_latest_floor_snapshot_wins():
    rows = [
        _row('p', 'ETHUSD', basis='no_data', best_distance=0.60, floor=0.55),
        _row('p', 'ETHUSD', basis='no_data', best_distance=0.72, floor=0.70),
    ]
    assert _aggregate(rows, '7d').rows[0].floor == 0.70


def test_envelopes_without_funnel_still_count_basis():
    # Pre-funnel envelopes (additive metadata) carry basis but no per_symbol_retrieval.
    rows = [('p', {'result': [{'symbol': 'BTCUSD', 'basis': 'no_data'}], 'metadata': {}})]
    row = _aggregate(rows, '7d').rows[0]
    assert row.no_data_passes == 1
    assert row.nearest_miss_min is None and row.floor is None
    assert not row.candidate                 # no miss/floor evidence → never flagged


def test_format_renders_rows_and_clean_week():
    silent = _aggregate([_row('p', 'ETHUSD', basis='no_data', best_distance=0.705,
                              floor=0.70)], '7d')
    text = format_no_data_report(silent)
    assert 'ETHUSD' in text and '⚠ candidate' in text and '100%' in text
    clean = _aggregate([_row('p', 'BTCUSD', kept=1)], '7d')
    assert 'all 1 symbols delivering' in format_no_data_report(clean)
