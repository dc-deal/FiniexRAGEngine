"""Tests for the retrieval floor profile (ISSUE_55 groundwork).

No database: the row shapes and the knee are built directly, so the *verdicts* — the part an
operator reads before deciding whether a floor is wrong — are covered everywhere. The SQL half is
exercised by running the report against a real corpus (see docs/testing.md).

The knee cases are the load-bearing ones. An unbounded knee search was measured putting
`Litecoin LTC` at 0.885, above its own median, because the widest gap in a full distance curve
sits out in the sparse tail where one distant article stands alone.
"""
from finiexragengine.core.observability.reports.floor_profile_report import (
    FloorProfileReport,
    FloorProfileRow,
    format_floor_profile_report,
    knee_of,
)


def _row(query='Cardano ADA', symbols=('ADAUSD',), nearest=0.706, p10=0.741, median=0.798,
         in_floor=0, foreign=0, knee=0.712, floor=0.70, passes=1075, no_data=410, miss=0.706,
         miss_avg=0.731):
    return FloorProfileRow(
        query_text=query, symbols=list(symbols), window_articles=312, nearest=nearest, p10=p10,
        median=median, in_floor=in_floor, foreign_in_floor=foreign, knee=knee, floor=floor,
        archive_passes=passes, archive_no_data=no_data, archive_nearest_miss=miss,
        archive_miss_avg=miss_avg)


def _report(rows):
    return FloorProfileReport(
        pipeline_id='crypto_sentiment', config_file='configs/pipelines/crypto_sentiment.json',
        model='text-embedding-3-small', floor=0.70, window_minutes=1440, window_articles=312,
        archive_label='7d', rows=list(rows), own_sources=['coindesk', 'decrypt'])


# --- the two verdicts, both threshold-free -------------------------------------------------

def test_a_query_whose_nearest_article_is_beyond_the_floor_is_starved():
    # The ADAUSD case: 24 articles in the window, all dropped, by six thousandths.
    assert _row(nearest=0.706, floor=0.70).starved is True
    assert _row(nearest=0.699, floor=0.70).starved is False


def test_a_query_whose_median_is_inside_the_floor_is_indiscriminate():
    # The DASHUSD shape: more than half the window counts as relevant to one symbol, which for a
    # symbol-specific query means the cut is not cutting.
    assert _row(median=0.688, floor=0.70).indiscriminate is True
    assert _row(median=0.702, floor=0.70).indiscriminate is False


def test_an_empty_window_produces_no_verdict_rather_than_a_false_one():
    empty = _row(nearest=None, p10=None, median=None, knee=None, miss=None, miss_avg=None)
    assert empty.starved is False and empty.indiscriminate is False
    assert 'n/a' in format_floor_profile_report(_report([empty]))


def test_the_miss_margin_is_what_sees_an_episodic_failure():
    # The case the live columns cannot show, measured 2026-08-19: ADAUSD's live `nearest` was
    # 0.648 — comfortably inside a 0.700 floor — while the archive says it lost 38 % of its
    # passes that week. The snapshot was taken on a good minute; the margin is the memory.
    healthy_right_now = _row(nearest=0.648, miss=0.706, miss_avg=0.731, floor=0.70)
    assert healthy_right_now.starved is False        # nothing wrong in THIS window
    assert round(healthy_right_now.missed_by, 3) == 0.006
    assert '+0.006' in format_floor_profile_report(_report([healthy_right_now]))


def test_a_symbol_that_never_starved_has_no_miss_margin():
    assert _row(passes=1075, no_data=0, miss=None, miss_avg=None).missed_by is None


def test_the_closest_and_the_mean_miss_are_both_reported():
    # They support opposite conclusions from the same 38 % failure rate: ADAUSD came within six
    # thousandths ONCE, but if the mean sits at +0.031 a floor moved by 0.010 rescues almost
    # nothing. Reporting only the minimum would argue for a change the data does not support.
    row = _row(miss=0.706, miss_avg=0.731, floor=0.70)
    assert round(row.missed_by, 3) == 0.006
    assert round(row.missed_by_avg, 3) == 0.031
    assert '+0.006/+0.031' in format_floor_profile_report(_report([row]))


def test_a_missing_mean_still_renders_the_closest():
    assert '+0.006/' in format_floor_profile_report(_report([_row(miss=0.706, miss_avg=None)]))


def test_shares_are_none_rather_than_zero_when_there_is_nothing_to_divide():
    assert _row(passes=0, no_data=0).no_data_share is None
    assert _row(in_floor=0, foreign=0).foreign_share is None
    assert _row(in_floor=40, foreign=10).foreign_share == 0.25


# --- the knee: ISSUE_55's deterministic cross-check ------------------------------------------

def test_the_knee_is_the_value_below_the_widest_gap():
    assert knee_of([0.40, 0.42, 0.43, 0.61, 0.62]) == 0.43


def test_the_knee_ignores_the_sparse_tail_when_bounded():
    # Measured while building: unbounded, the tail wins and the "floor candidate" lands above the
    # median, which is worse than useless — it is a plausible-looking wrong number.
    curve = [0.40, 0.42, 0.43, 0.61, 0.62, 0.98]
    assert knee_of(curve) == 0.62                     # the tail gap, 0.62 -> 0.98
    assert knee_of(curve, within=0.65) == 0.43        # the real separation


def test_the_knee_needs_two_samples_in_range():
    assert knee_of([]) is None
    assert knee_of([0.5]) is None
    assert knee_of([0.5, 0.9], within=0.6) is None    # only one survives the bound


def test_the_knee_prefers_the_widest_gap_not_the_first():
    assert knee_of([0.30, 0.34, 0.35, 0.50]) == 0.35


# --- rendering -------------------------------------------------------------------------------

def test_the_render_marks_only_the_rows_that_deviate():
    healthy = _row(query='Bitcoin BTC', symbols=('BTCUSD',), nearest=0.483, median=0.702,
                   in_floor=128, passes=1075, no_data=0)
    text = format_floor_profile_report(_report([healthy, _row()]))
    assert '✗' in text                                 # the starved ADAUSD row
    assert text.count('⚠') == 1                        # legend only — no row is indiscriminate
    assert '38.1 %' in text                            # 410/1075 rendered as the archive share
    assert '0.0 %' in text                             # a never-starved symbol still shows a share


def test_a_symbol_with_no_archive_rows_renders_a_dash_not_a_zero():
    # build_no_data_report only returns symbols that had a no-data pass, so "absent" must be
    # distinguishable from "measured at zero".
    text = format_floor_profile_report(_report([_row(passes=0, no_data=0)]))
    assert '—' in text


def test_the_summary_counts_both_verdicts():
    text = format_floor_profile_report(_report([
        _row(),                                                    # starved
        _row(query='Dash cryptocurrency', symbols=('DASHUSD',), nearest=0.612, median=0.688,
             in_floor=47, foreign=19, passes=1075, no_data=0),     # indiscriminate
    ]))
    assert '1 starved' in text and '1 indiscriminate' in text


def test_no_queries_is_a_stated_empty_answer():
    assert 'no symbol queries configured' in format_floor_profile_report(_report([]))
