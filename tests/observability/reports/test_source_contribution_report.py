"""Tests for the source contribution report (ISSUE_82 finding 9).

No database: the envelope walk and the arithmetic are the parts that decide what the table says,
and both are DB-free by construction — the same split `no_data_report._aggregate` uses.

What the walk has to get right is *distinctness*: the question is whether a feed's article reached
a prompt at all, not how often it was reused. An article retrieved for nine symbols across two
hundred passes is one contribution, and counting it two hundred times would make a feed's rank a
function of pass cadence.
"""
from finiexragengine.core.observability.reports.source_contribution_report import (
    SourceContributionReport,
    SourceContributionRow,
    collect_citations,
    format_source_contribution_report,
)


def _envelope(*results):
    return {'pipeline_id': 'crypto_sentiment', 'result': list(results)}


def _result(symbol, article_ids, is_breaking=False):
    return {'symbol': symbol, 'is_breaking': is_breaking,
            'sources': [{'article_id': aid, 'url': f'https://x/{aid}'} for aid in article_ids]}


# --- the envelope walk ------------------------------------------------------------------------

def test_an_article_cited_many_times_counts_once():
    rows = [('crypto_sentiment', _envelope(_result('BTCUSD', ['a1', 'a2']),
                                           _result('ETHUSD', ['a1'])))] * 5
    cited, breaking = collect_citations(rows)
    assert cited == {'a1', 'a2'}
    assert breaking == set()


def test_breaking_citations_are_tracked_separately():
    cited, breaking = collect_citations([
        ('crypto_sentiment', _envelope(_result('SOLUSD', ['a1'], is_breaking=True),
                                       _result('ADAUSD', ['a2'])))])
    assert cited == {'a1', 'a2'}
    assert breaking == {'a1'}          # only the source behind the breaking verdict


def test_a_malformed_or_pre_issue_2_source_is_skipped_not_crashed():
    cited, _ = collect_citations([
        ('crypto_sentiment', _envelope({'symbol': 'BTCUSD', 'sources': [
            {'url': 'https://x/no-id'},          # no article_id (older shape)
            None,                                 # a null slot
            {'article_id': 'a1'},
        ]})),
        ('crypto_sentiment', {'result': None}),   # no results at all
        ('crypto_sentiment', 'not-a-dict'),       # not an envelope
    ])
    assert cited == {'a1'}


# --- the arithmetic ---------------------------------------------------------------------------

def _row(source_id='cryptonews', weight=0.8, articles=136, cited=20, breaking=7,
         enabled=True, median=0.35):
    return SourceContributionRow(source_id=source_id, weight=weight, articles=articles,
                                 cited=cited, breaking_cited=breaking, enabled=enabled,
                                 median_citation_share=median)


def test_citation_share_is_none_when_the_feed_published_nothing():
    assert _row(articles=0, cited=0).citation_share is None
    assert _row(articles=200, cited=50).citation_share == 0.25


def test_a_feed_that_published_and_was_never_cited_is_named():
    # cnbc_forex, measured live 2026-08-19: 17 articles, cited zero times, weight 1.00. The first
    # version of this marker used an absolute rate threshold calibrated on a stale dev corpus and
    # stayed silent on exactly this row.
    assert _row(articles=17, cited=0).never_cited is True
    assert _row(articles=17, cited=0).under_used is False   # one marker, not two


def test_a_feed_below_the_judgeable_count_is_never_flagged():
    # ecb_press publishes a statement a week; '0 of 1 cited' says nothing about the feed.
    assert _row(articles=1, cited=0).never_cited is False
    assert _row(articles=9, cited=0).never_cited is False
    assert _row(articles=3, cited=1, median=0.60).under_used is False


def test_low_is_measured_against_the_sets_own_median():
    # 33 % is poor in a set whose median is 62 % and unremarkable in one whose median is 33 %.
    assert _row(articles=100, cited=10, median=0.62).under_used is True     # 10 % vs 31 % bar
    assert _row(articles=100, cited=10, median=0.20).under_used is False    # 10 % vs 10 % bar


def test_a_well_cited_feed_is_not_flagged():
    assert _row(articles=136, cited=20, median=0.10).under_used is False


# --- rendering ---------------------------------------------------------------------------------

def _report(rows, envelopes=90, unknown=0):
    return SourceContributionReport(source_set_id='crypto_news', since_label='7d',
                                    rows=list(rows), envelopes=envelopes, cited_unknown=unknown)


def test_the_render_shows_the_configured_weight_beside_what_was_observed():
    # The measured inversion this report exists to make visible: the down-rated feed leads.
    # Live 2026-08-19, forex: actionforex (0.80) contributed 124 cited articles, cnbc_forex
    # (1.00) contributed none.
    text = format_source_contribution_report(_report([
        _row(source_id='actionforex', weight=0.8, articles=180, cited=124, breaking=9,
             median=0.33),
        _row(source_id='cnbc_forex', weight=1.0, articles=17, cited=0, breaking=0, median=0.33),
    ]))
    assert '0.80' in text and '1.00' in text
    assert '68.9 %' in text and '0.0 %' in text
    assert 'published 17, never cited' in text
    assert 'set median citation rate: 33.0 %' in text


def test_a_disabled_feed_keeps_its_row():
    text = format_source_contribution_report(_report([
        _row(source_id='cryptoslate', enabled=False, articles=0, cited=0)]))
    assert '[disabled]' in text          # marked, not dropped — its history still happened


def test_an_all_time_window_reads_as_a_window_not_as_a_duration():
    report = _report([_row()])
    report.since_label = 'all-time'
    assert '· all-time' in format_source_contribution_report(report)
    assert 'last all-time' not in format_source_contribution_report(report)


def test_articles_that_outlived_the_corpus_are_reported_not_hidden():
    text = format_source_contribution_report(_report([_row()], unknown=12))
    assert '12 cited article(s) no longer in the corpus' in text


def test_no_feeds_is_a_stated_empty_answer():
    assert 'no feeds configured' in format_source_contribution_report(_report([]))
