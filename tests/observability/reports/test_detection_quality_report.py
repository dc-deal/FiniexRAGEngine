"""Detection quality (ISSUE_106) — what the detector flagged and on what evidence.

Tests `aggregate_detection_quality` directly with synthetic corpus rows, so no DB is needed.

The report exists because `by_trigger` counts flags without judging them. Its statement is the
**duplication ratio**, and the cases below are built around the two shapes it has to tell apart:
three outlets carrying one story, and one feed carrying nine copies of its own template.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.detection_quality_report import (
    aggregate_detection_quality,
    format_detection_quality_report,
)

_TS = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)


def _row(article_id, *, source_id='coindesk', title='a story', trigger='cluster',
         importance=2, articles=None, feeds=None, minutes=0):
    """One flagged corpus row, in the column order the builder selects."""
    return (article_id, source_id, title, trigger, importance, articles, feeds,
            _TS - timedelta(minutes=minutes))


def test_a_cross_feed_story_and_a_single_feed_template_are_told_apart():
    """The report's whole reason to exist, in one comparison.

    Both neighbourhoods are the same size by the article count — which is exactly why that count
    could not be tuned. The ratio separates them: 1.0 against 9.0.
    """
    story = aggregate_detection_quality(
        [_row('a', articles=3, feeds=3)], '7d')
    template = aggregate_detection_quality(
        [_row('b', source_id='actionforex', title='EUR/USD Daily Outlook',
              articles=9, feeds=1)], '7d')

    assert story.cluster_row.duplication == 1.0 and not story.cluster_row.suspect
    assert template.cluster_row.duplication == 9.0 and template.cluster_row.suspect
    assert template.examples[0].single_feed and not story.examples[0].single_feed


def test_duplication_is_pooled_over_the_window_not_averaged_per_flag():
    """A mixed window: one honest flag and one template.

    Averaging the two ratios (1.0 and 9.0) gives 5.0, which describes neither. Pooling both sides
    gives 12/4 = 3.0 — what the window actually delivered, and the number a threshold is judged on.
    """
    report = aggregate_detection_quality(
        [_row('a', articles=3, feeds=3), _row('b', articles=9, feeds=1)], '7d')

    assert report.cluster_row.duplication == 3.0


def test_the_keyword_path_reports_no_neighbourhood_rather_than_zero():
    """It consulted none. A 0 would claim an empty cluster was looked at, which is the distinction
    NULL exists to keep — the same one `detection_trigger` draws between a category and an
    absence."""
    report = aggregate_detection_quality(
        [_row('a', trigger='keyword', importance=3, articles=None, feeds=None)], '7d')
    row = report.rows[0]

    assert (row.trigger, row.flags, row.high) == ('keyword', 1, 1)
    assert row.measured == 0
    assert row.duplication is None and row.feeds_median is None
    assert '—' in format_detection_quality_report(report)


def test_a_flag_from_before_the_trigger_column_is_counted_but_never_attributed():
    """Folding it into a path would invent evidence for whichever one is being judged."""
    report = aggregate_detection_quality(
        [_row('a', trigger=None), _row('b', trigger='cluster', articles=3, feeds=3)], '7d')

    assert report.unattributed == 1
    assert [row.trigger for row in report.rows] == ['cluster']
    assert report.total_flags == 2
    assert 'before the trigger column existed' in format_detection_quality_report(report)


def test_tiers_split_on_importance_and_the_spread_reports_its_own_population():
    report = aggregate_detection_quality([
        _row('a', importance=3, articles=4, feeds=4),
        _row('b', importance=2, articles=3, feeds=3),
        _row('c', importance=2, articles=6, feeds=2),
    ], '7d')
    row = report.rows[0]

    assert (row.flags, row.high, row.mid) == (3, 1, 2)
    assert (row.feeds_min, row.feeds_median, row.feeds_max) == (2, 3.0, 4)
    assert row.measured == 3


def test_examples_are_the_newest_flags_and_are_capped():
    """Rows arrive newest first, and a calibration question is about what the detector is doing
    now — the oldest flags in a window are the ones a threshold change has already superseded."""
    rows = [_row(f'a{i}', title=f'story {i}', articles=3, feeds=3, minutes=i) for i in range(6)]
    report = aggregate_detection_quality(rows, '7d', example_limit=2)

    assert [example.title for example in report.examples] == ['story 0', 'story 1']


def test_flags_per_feed_surface_one_feed_dominating():
    rows = ([_row(f'x{i}', source_id='actionforex', articles=9, feeds=1) for i in range(4)]
            + [_row('y', source_id='coindesk', articles=3, feeds=3)])
    report = aggregate_detection_quality(rows, '7d')

    assert [(row.source_id, row.flags) for row in report.sources] == [
        ('actionforex', 4), ('coindesk', 1)]


def test_a_set_with_the_cluster_path_off_says_so_instead_of_showing_a_gap():
    """"Off by decision" and "off by arithmetic" are different states (ISSUE_106).

    Without the line, `forex_news` contributing no cluster flags reads as a threshold somebody
    should go fix — and fixing it there means loosening the article count into 27 HIGH flags a week
    out of one feed's daily template.
    """
    report = aggregate_detection_quality(
        [_row('a', trigger='keyword', importance=3)], '7d', disabled_sets=['forex_news'])
    rendered = format_detection_quality_report(report)

    assert 'forex_news · cluster path OFF by config' in rendered
    assert 'a decision, not a gap' in rendered


def test_an_empty_window_states_it_rather_than_rendering_an_empty_table():
    rendered = format_detection_quality_report(aggregate_detection_quality([], '7d'))

    assert 'neither path fired' in rendered
