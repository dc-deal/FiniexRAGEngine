"""Detection quality (ISSUE_106) — what the detector flagged and on what evidence.

Tests `aggregate_detection_quality` directly with synthetic corpus rows, so no DB is needed.

The report exists because `by_trigger` counts flags without judging them. Its statement is the
**duplication ratio**, and the cases below are built around the two shapes it has to tell apart:
three outlets carrying one story, and one feed carrying nine copies of its own template.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.corpus_text_report import KeywordSet
from finiexragengine.core.observability.reports.detection_quality_report import (
    aggregate_detection_quality,
    format_detection_quality_report,
)

_TS = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)


def _row(article_id, *, source_id='coindesk', title='a story', trigger='cluster',
         importance=2, articles=None, feeds=None, minutes=0, keywords=None):
    """One flagged corpus row, in the column order the builder selects.

    `keywords` defaults to None — the cluster path's shape, and also what every keyword flag made
    before migration 014 carries.
    """
    return (article_id, source_id, title, trigger, importance, articles, feeds,
            _TS - timedelta(minutes=minutes), keywords)


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


# --- the keyword path's evidence (migration 014) -------------------------------------------

def _kwset(source_set_id='crypto_news', keywords=()):
    """The configured vocabulary the report compares the corpus against."""
    return KeywordSet(source_set_id=source_set_id, keywords=tuple(keywords),
                      keyword_source_weight=0.9, weights={})


def test_which_term_fired_is_reported_per_term_not_as_one_keyword_total():
    """The defect the column closes: 'the keyword path made N flags' is not a tunable sentence.

    A vocabulary decision is taken per term — keep `hack`, drop `SEC` — and the aggregate cannot
    inform it however large it gets.
    """
    report = aggregate_detection_quality(
        [_row('a', source_id='coindesk', trigger='keyword', keywords=['hack']),
         _row('b', source_id='decrypt', trigger='keyword', keywords=['hack']),
         _row('c', source_id='sec_press', trigger='keyword', importance=3, keywords=['SEC'])],
        '7d')

    assert [(row.term, row.flags, row.feed_count) for row in report.terms] == [
        ('hack', 2, 2), ('SEC', 1, 1)]
    assert report.terms[1].high == 1 and report.terms[0].high == 0


def test_a_term_confined_to_one_publisher_is_marked_and_the_feed_is_named():
    """The keyword analogue of the duplication ratio.

    #46 measured the case: the bare token `SEC` fires on 25 of 25 SEC press releases, which is a
    property of that feed's template rather than a crisis signal. Marked, never judged — a central
    bank legitimately is the only publisher of its own decision.
    """
    report = aggregate_detection_quality(
        [_row(str(n), source_id='sec_press', trigger='keyword', keywords=['SEC'])
         for n in range(25)]
        + [_row('x', source_id='coindesk', trigger='keyword', keywords=['hack']),
           _row('y', source_id='decrypt', trigger='keyword', keywords=['hack'])],
        '7d')
    by_term = {row.term: row for row in report.terms}

    assert by_term['SEC'].single_feed and by_term['SEC'].only_feed == 'sec_press'
    assert not by_term['hack'].single_feed
    assert '⚠ only sec_press' in format_detection_quality_report(report)


def test_a_configured_term_that_never_fired_is_named_with_its_set():
    """The case that is invisible without the config: a term that CANNOT match.

    `monetary policy decision` matches zero rows against the ECB's own `Monetary policy decisions`,
    because the matcher anchors `\\b…\\b` over an escaped config value. Nothing reports a miss, so
    the only trace is a term that stays silent — which is why silence is rendered rather than
    dropped, and why it is explicitly not called a fault.
    """
    report = aggregate_detection_quality(
        [_row('a', trigger='keyword', keywords=['hack'])], '7d',
        keyword_sets=[_kwset(keywords=['hack', 'monetary policy decision'])])

    assert [(row.source_set_id, row.term) for row in report.silent_terms] == [
        ('crypto_news', 'monetary policy decision')]
    text = format_detection_quality_report(report)
    # Reported as a SHARE of what the set declares: "1 of 2" says one term of the vocabulary was
    # quiet, where a bare "1" cannot distinguish that from a whole vocabulary contributing nothing.
    assert 'crypto_news · 1 of 2 configured term(s) silent: monetary policy decision' in text
    assert 'check the spelling against a feed' in text


def test_a_keyword_flag_without_a_recorded_term_is_counted_as_unrecorded_not_as_silent():
    """Pre-migration flags know their path and not their word.

    Folding them into "matched nothing" would understate every term at once and make the first
    window after deploy look like a vocabulary collapse.
    """
    report = aggregate_detection_quality(
        [_row('a', trigger='keyword', keywords=None)], '7d',
        keyword_sets=[_kwset(keywords=['hack'])])

    assert report.terms_unrecorded == 1
    assert report.terms == []
    assert 'flagged before the column existed' in format_detection_quality_report(report)


def test_a_cluster_flag_never_contributes_to_the_vocabulary_breakdown():
    """Even when its article contained a term — the attribution went to the burst.

    The mirror of the rule that keeps `cluster_articles` out of a keyword flag.
    """
    report = aggregate_detection_quality(
        [_row('a', trigger='cluster', articles=6, feeds=6, keywords=['halt'])], '7d')

    assert report.terms == [] and report.terms_unrecorded == 0


def test_the_section_is_absent_when_no_vocabulary_is_configured_or_fired():
    """A set with no keywords must not grow an empty table that reads as a gap."""
    report = aggregate_detection_quality([_row('a', articles=3, feeds=3)], '7d')

    assert 'keyword vocabulary' not in format_detection_quality_report(report)


def test_one_sets_vocabulary_can_never_hide_another_behind_the_cap():
    """The defect found reading the first live report (2026-09-12).

    Silence was pooled and capped at eight names. Sorted by set, the first set filled the cap and
    every later one vanished into `+N more` — on production that meant all eight names were
    `crypto_news` and `forex_news`'s entire vocabulary was invisible. Capping PER SET is what makes
    the line honest: every set that has something to say gets a line of its own.
    """
    report = aggregate_detection_quality(
        [], '7d',
        keyword_sets=[_kwset('crypto_news', [f'c{n:02d}' for n in range(11)]),
                      _kwset('forex_news', ['abandons peg', 'devaluation'])])
    text = format_detection_quality_report(report)

    assert 'crypto_news · 11 of 11 configured term(s) silent' in text
    # The set that would have been swallowed: named, with its own terms, not a counter.
    assert 'forex_news · 2 of 2 configured term(s) silent: abandons peg, devaluation' in text


def test_a_window_that_predates_the_column_says_the_silence_is_the_migration():
    """The claim names its population — the same rule `measured` and `_count_label` follow.

    Otherwise the first window after deploy reads as a vocabulary collapse: every configured term
    looks silent, when in fact nothing in the window could be attributed at all.
    """
    report = aggregate_detection_quality(
        [_row('a', trigger='keyword', keywords=None)], '7d',
        keyword_sets=[_kwset(keywords=['hack', 'exploit'])])
    text = format_detection_quality_report(report)

    assert 'NOTHING could be attributed' in text
    assert 'this silence is the migration, not the vocabulary' in text
    # And the ordinary caveat is NOT also printed — two explanations for one line is worse than one.
    assert 'a quiet window explains silence' not in text


def test_the_term_table_does_not_render_columns_that_are_structurally_constant():
    """`_tier` returns HIGH for every keyword verdict, so MID is always 0 and HIGH always == flags.

    Rendering them invites the reader to treat two constants as measurements. What replaces them is
    the fact itself, which is the useful half: every row here is a breaking wake.
    """
    report = aggregate_detection_quality(
        [_row('a', trigger='keyword', importance=3, keywords=['hack'])], '7d')
    text = format_detection_quality_report(report)

    assert 'every keyword flag is HIGH by construction' in text
    header = next(line for line in text.splitlines() if line.lstrip().startswith('term '))
    assert 'MID' not in header and 'HIGH' not in header
