"""Keyword sweep (ISSUE_121) — needs a reachable Postgres (skipped otherwise).

What the report exists to prevent is a vocabulary written into the one gate that needs no
corroboration, with nothing able to say what it does until production says it. So what is asserted
here is the part that can be wrong while looking right:

- the pattern is the DETECTOR's construction, not a second one — a sweep describing a different
  matcher is worse than no sweep;
- a zero is a finding: `monetary policy decision` matches nothing while the ECB titles every
  decision *"Monetary policy decisions"*, and the report has to say so rather than leave a blank;
- `hits` and `gated` are two numbers because a term that fires only below `keyword_source_weight`
  is dead vocabulary, and that is invisible in a raw count.

Seeded through the real `PgVectorStore.upsert`, like `test_corpus_text_report`, so the rows under
test are written by the code that writes them in production.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from finiexragengine.core.observability.reports.corpus_text_report import KeywordSet
from finiexragengine.core.observability.reports.keyword_sweep_report import (
    build_keyword_sweep_report,
    format_keyword_sweep_report,
)
from finiexragengine.core.pipeline import breaking_detector
from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.core.sources.article_normalizer import ArticleNormalizer
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig
from finiexragengine.utils.keyword_pattern import build_keyword_pattern

_DIMS = 1536
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_SINCE = _NOW - timedelta(days=14)

# `ecb_press` clears the gate and flags on its own; `aggregator` does not.
_SET = KeywordSet(source_set_id='forex_news',
                  keywords=('rate decision', 'intervention'),
                  keyword_source_weight=0.9,
                  weights={'ecb_press': 1.0, 'aggregator': 0.6})


def _vec() -> List[float]:
    return [1.0] + [0.0] * (_DIMS - 1)


@pytest.fixture
def store(clean_db: str) -> PgVectorStore:
    return PgVectorStore(VectorStoreConfig(), clean_db, dimensions=_DIMS,
                         embedding_model='test-embed')


def _seed(store: PgVectorStore, article_id: str, title: str, *, summary: str = '',
          source_id: str = 'ecb_press', normalize: bool = True,
          published_at: Optional[datetime] = None) -> None:
    article = Article(article_id=article_id, source_id=source_id, source_weight=1.0,
                      url=f'https://example.test/{article_id}', title=title, summary=summary,
                      language='en', published_at=published_at or _NOW, fetched_at=_NOW)
    if normalize:
        ArticleNormalizer().apply(article)
    store.upsert([article], [_vec()])


def _sweep(database_url: str, **kwargs):
    return build_keyword_sweep_report(database_url, _SINCE, keyword_set=_SET,
                                      since_label='14d', **kwargs)


def _row(report, term: str):
    return next(row for row in report.terms if row.term == term)


# --- one construction, shared with the detector -------------------------------------------------

def test_the_sweep_matches_with_the_detector_s_own_pattern_builder() -> None:
    """Asserted mechanically rather than by eye: two constructions would drift, and the report
    would then describe a matcher that is not the one running at ingest."""
    assert breaking_detector.build_keyword_pattern is build_keyword_pattern


def test_the_shared_pattern_is_word_bounded_case_insensitive_and_never_a_regex() -> None:
    pattern = build_keyword_pattern(['SEC', 'rate decision', 'a.b'])

    assert pattern is not None
    assert pattern.search('The SEC filed today') and not pattern.search('It took 30 seconds')
    assert pattern.search('ECB Rate Decision due')          # case-insensitive, as at ingest
    assert pattern.search('a.b matched') and not pattern.search('axb not matched')
    assert build_keyword_pattern([]) is None                # no vocabulary is a state, not a match


# --- the zero that is a finding -----------------------------------------------------------------

def test_a_term_that_matches_nothing_reports_its_plural_when_the_feeds_publish_it(
        store: PgVectorStore, clean_db: str) -> None:
    """The defect that motivated the issue: the ECB titles every decision in the PLURAL."""
    _seed(store, 'ecb-1', 'Monetary policy decisions')
    _seed(store, 'ecb-2', 'ECB: Monetary policy decisions and the outlook')

    report = _sweep(clean_db, terms=['monetary policy decision'])

    row = _row(report, 'monetary policy decision')
    assert row.hits == 0 and row.gated == 0 and row.dead
    assert row.plural_probe == 'monetary policy decisions' and row.plural_hits == 2
    rendered = format_keyword_sweep_report(report, width=100)
    assert '`monetary policy decision` matches 0 rows' in rendered
    assert '`monetary policy decisions` matches 2' in rendered


def test_a_zero_without_a_matching_plural_stays_a_plain_zero(
        store: PgVectorStore, clean_db: str) -> None:
    _seed(store, 'ecb-1', 'Monetary policy decisions')

    report = _sweep(clean_db, terms=['emergency cut'])

    row = _row(report, 'emergency cut')
    assert row.hits == 0 and row.plural_hits == 0 and not row.plural_probe
    assert 'matches 0 rows' not in format_keyword_sweep_report(report, width=100)


# --- two counts, because the gate decides --------------------------------------------------------

def test_a_term_firing_only_below_the_gate_is_counted_and_called_dead(
        store: PgVectorStore, clean_db: str) -> None:
    _seed(store, 'agg-1', 'Analysts expect a rate decision today', source_id='aggregator')

    report = _sweep(clean_db, terms=['rate decision'])

    row = _row(report, 'rate decision')
    assert (row.hits, row.gated, row.feed_count) == (1, 0, 1)
    assert row.dead and report.gated_hits == 0
    assert 'dead vocabulary' in format_keyword_sweep_report(report, width=100)


def test_the_example_prefers_a_headline_that_could_actually_have_flagged(
        store: PgVectorStore, clean_db: str) -> None:
    """An ungated headline beside a term's counts invites the wrong conclusion about the term."""
    _seed(store, 'agg-1', 'Aggregator: rate decision preview', source_id='aggregator',
          published_at=_NOW - timedelta(hours=2))
    _seed(store, 'ecb-1', 'ECB rate decision published')

    row = _row(_sweep(clean_db, terms=['rate decision']), 'rate decision')

    assert (row.hits, row.gated) == (2, 1)
    assert row.example == 'ECB rate decision published'


# --- what the sweep is pointed at ----------------------------------------------------------------

def test_without_terms_the_configured_vocabulary_is_swept(
        store: PgVectorStore, clean_db: str) -> None:
    """The same surface answers 'what would this list do' and 'what is our list doing'."""
    _seed(store, 'ecb-1', 'ECB rate decision published')
    _seed(store, 'snb-1', 'SNB announces intervention in FX markets')

    report = _sweep(clean_db)

    assert report.from_config
    assert {row.term for row in report.terms} == set(_SET.keywords)
    assert _row(report, 'rate decision').gated == 1
    assert _row(report, 'intervention').gated == 1
    assert report.feeds_total == 2 and report.feeds_at_gate == 1


def test_the_normaliser_selector_narrows_the_corpus_to_one_treatment(
        store: PgVectorStore, clean_db: str) -> None:
    """`corpus_text` owns served-vs-stored; this only compares like with like when asked to."""
    _seed(store, 'ecb-1', 'ECB rate decision published', normalize=True)
    _seed(store, 'ecb-2', 'Another ECB rate decision', normalize=False)

    stamped = _sweep(clean_db, terms=['rate decision'], normalizer='v1')
    everything = _sweep(clean_db, terms=['rate decision'])

    assert _row(stamped, 'rate decision').hits == 1
    assert _row(everything, 'rate decision').hits == 2


def test_a_window_with_no_articles_is_an_empty_report_not_a_crash(clean_db: str) -> None:
    report = _sweep(clean_db, terms=['rate decision'])

    assert report.articles == 0 and _row(report, 'rate decision').hits == 0
    assert 'keyword sweep · forex_news' in format_keyword_sweep_report(report, width=100)
