"""Corpus text report (ISSUE_112) — the treatment census and the phantom keyword class.

DB-backed, because every number here is a property of stored rows: which treatment produced them,
what survived it, and what the kept raw copy says was removed. Seeded through the real
`PgVectorStore.upsert`, so the columns under test are written by the code that writes them in
production rather than by a hand-rolled INSERT that could drift from it.

The case that earns the report its place is `test_a_stamped_row_that_still_carries_markup_is_flagged`:
a census that can only render the happy path is decoration. Its failure state has to be visible, or
nobody can tell "the treatment works" from "the report cannot see that it does not".
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest

from finiexragengine.core.observability.reports.corpus_text_report import (
    UNSTAMPED,
    KeywordSet,
    build_corpus_text_report,
    format_corpus_text_report,
)
from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.core.sources.article_normalizer import ArticleNormalizer
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig

_DIMS = 1536
_NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
_SINCE = _NOW - timedelta(days=7)

_KEYWORDS = ('hack', 'exploit', 'collapse', 'lawsuit')
# cointelegraph clears the gate and therefore flags HIGH on its own; cryptonews does not.
_SET = KeywordSet(source_set_id='crypto_news', keywords=_KEYWORDS, keyword_source_weight=0.9,
                  weights={'cointelegraph': 1.0, 'cryptonews': 0.8})


def _vec() -> List[float]:
    return [1.0] + [0.0] * (_DIMS - 1)


@pytest.fixture
def store(clean_db: str) -> PgVectorStore:
    return PgVectorStore(VectorStoreConfig(), clean_db, dimensions=_DIMS,
                         embedding_model='test-embed')


def _seed(store: PgVectorStore, article_id: str, title: str, summary: str, *,
          source_id: str = 'cointelegraph', weight: float = 1.0,
          normalize: bool = True, stamp: Optional[str] = None,
          fetched_at: Optional[datetime] = None) -> Article:
    """Store one article, optionally through the real treatment.

    `normalize=False` with an explicit `stamp` is how a BROKEN treatment is simulated: the row
    claims a profile while its text still carries what that profile removes.
    """
    article = Article(article_id=article_id, source_id=source_id, source_weight=weight,
                      url=f'https://example.test/{article_id}', title=title, summary=summary,
                      language='en', published_at=_NOW, fetched_at=fetched_at or _NOW)
    if normalize:
        ArticleNormalizer().apply(article)
    if stamp is not None:
        article.text_normalizer = stamp
    store.upsert([article], [_vec()])
    return article


def _report(database_url: str, **kwargs):
    return build_corpus_text_report(database_url, _SINCE, since_label='7d',
                                    keyword_sets=[_SET], **kwargs)


# --- the census -------------------------------------------------------------------------------

def test_the_census_splits_by_treatment_and_counts_surviving_carriers(clean_db, store):
    _seed(store, 'a', '<b>Hack</b> confirmed', '<p>Funds &amp; keys</p>')          # v1
    _seed(store, 'b', 'Clean headline', 'Clean summary')                            # v1, clean
    _seed(store, 'c', '<p>Legacy</p>', 'Untouched &amp; raw',
          normalize=False, stamp=None)                                              # unstamped

    report = _report(clean_db)
    by_profile = {t.profile: t for t in report.treatments}

    assert report.articles == 3
    assert by_profile['v1'].articles == 2
    assert by_profile['v1'].clean                       # the treatment did its job
    assert by_profile[UNSTAMPED].articles == 1
    assert by_profile[UNSTAMPED].with_markup == 1
    assert by_profile[UNSTAMPED].with_entities == 1


def test_a_stamped_row_that_still_carries_markup_is_flagged(clean_db, store):
    """The failure state. A report that can only render success proves nothing.

    The row claims profile 'v1' while its text still holds a tag — which is what a normaliser that
    silently stopped running looks like from the outside. `dirty_stamped` is the assertion, and the
    rendering has to say so rather than printing the reassuring ✓ line.
    """
    _seed(store, 'broken', '<b>Hack</b> confirmed', 'summary',
          normalize=False, stamp='v1')

    report = _report(clean_db)
    dirty = report.dirty_stamped

    assert [t.profile for t in dirty] == ['v1']
    assert dirty[0].with_markup == 1
    text = format_corpus_text_report(report, width=100)
    assert 'the normaliser is not doing what it claims' in text
    assert 'carrier-free' not in text


def test_a_working_treatment_says_so_explicitly(clean_db, store):
    _seed(store, 'a', '<b>Hack</b>', '<p>x</p>')
    text = format_corpus_text_report(_report(clean_db), width=100)
    assert '✓ every one of the 1 stamped articles is carrier-free' in text


def test_the_unstamped_slice_is_an_absence_not_a_profile(clean_db, store):
    """A row from before the treatment existed must never be folded into one."""
    _seed(store, 'legacy', '<p>Old</p>', 'text', normalize=False, stamp=None)
    report = _report(clean_db)
    assert [t.profile for t in report.treatments] == [UNSTAMPED]
    assert report.stamped == 0
    assert report.dirty_stamped == []       # an unstamped carrier is not a treatment failure


# --- what it removed --------------------------------------------------------------------------

def test_removal_is_measured_within_the_row_against_its_own_original(clean_db, store):
    dirty = _seed(store, 'a', '<b>Hack</b>', 'plain')
    _seed(store, 'b', 'Clean', 'Clean')

    report = _report(clean_db)
    served = len(dirty.title_raw) + len(dirty.summary_raw or dirty.summary)
    stored = len(dirty.title) + len(dirty.summary)

    assert report.removal.rows == 2                 # both stamped rows are the denominator
    assert report.removal.rows_changed == 1         # only one actually carried anything
    assert report.removal.chars_removed == served - stored
    assert 0 < report.removal.removed_share < 1


def test_a_corpus_that_arrived_clean_reports_no_removal(clean_db, store):
    _seed(store, 'a', 'Clean headline', 'Clean summary')
    report = _report(clean_db)
    assert report.removal.rows == 1
    assert report.removal.rows_changed == 0
    assert report.removal.chars_removed == 0


# --- the keyword fast path --------------------------------------------------------------------

def test_a_keyword_that_lives_only_inside_markup_is_a_phantom_hit(clean_db, store):
    """The exact class ISSUE_112 was opened on — a CDN filename, on a weight-1.0 feed."""
    _seed(store, 'phantom', 'Bitcoin steadies',
          '<p><img src="https://cdn.test/covers/courtroom-lawsuit-justice.png" /></p>',
          normalize=False, stamp=None)

    report = _report(clean_db)
    assert [p.source_id for p in report.phantoms] == ['cointelegraph']
    phantom = report.phantoms[0]
    assert phantom.phantom_hits == 1
    assert phantom.prose_hits == 0
    assert phantom.self_flags is True            # weight 1.0 >= gate 0.9
    assert report.phantom_self_flagging == 1


def test_a_keyword_in_prose_is_never_a_phantom_hit(clean_db, store):
    """The other half — de-noising must not cost a genuine hit."""
    _seed(store, 'real', 'Bridge hack drains 8,000 ETH', 'Funds are gone.',
          normalize=False, stamp=None)
    report = _report(clean_db)
    assert report.phantoms == []                 # nothing to report: the hit survives normalising


def test_a_phantom_below_the_gate_is_listed_but_not_counted_as_self_flagging(clean_db, store):
    """Weight decides whether a phantom hit could flag HIGH on its own — the number that matters."""
    _seed(store, 'below', 'Markets steady',
          '<a href="https://cryptonews.test/etf-flows-collapse-below-1b/">Flows</a>',
          source_id='cryptonews', weight=0.8, normalize=False, stamp=None)

    report = _report(clean_db)
    assert report.phantom_total == 1
    assert report.phantom_self_flagging == 0     # 0.8 < 0.9
    text = format_corpus_text_report(report, width=110)
    assert 'cryptonews' in text
    assert 'raised an article to HIGH' not in text


def test_a_feed_no_configured_set_claims_is_named_rather_than_dropped(clean_db, store):
    """Its gate is unknown, so its hits cannot be judged — saying so beats silent exclusion."""
    _seed(store, 'x', 'Hack confirmed', 'text', source_id='someone_else', normalize=False)
    report = _report(clean_db)
    assert report.orphan_sources == ['someone_else']
    assert 'not judged' in format_corpus_text_report(report, width=110)


def test_an_empty_phantom_table_says_it_was_checked(clean_db, store):
    """"Checked and none found" and "nothing ran" must not render identically."""
    _seed(store, 'a', 'Clean headline', 'Clean summary')
    text = format_corpus_text_report(_report(clean_db), width=110)
    assert 'checked against crypto_news' in text
    assert 'every keyword hit in the corpus survives normalising' in text


def test_without_a_vocabulary_the_report_says_it_did_not_check(clean_db, store):
    _seed(store, 'a', 'Hack confirmed', 'text', normalize=False)
    report = build_corpus_text_report(clean_db, _SINCE, since_label='7d', keyword_sets=[])
    text = format_corpus_text_report(report, width=110)
    assert 'not checked (no detection vocabulary was supplied)' in text


# --- the flow half ----------------------------------------------------------------------------

def test_the_window_narrows_the_flow_but_not_the_census(clean_db, store):
    """The window is the flow's; the census is a property of stored rows, not of a time slice."""
    _seed(store, 'old', '<b>Old</b>', 'text', fetched_at=_NOW - timedelta(days=30))
    _seed(store, 'new', '<b>New</b>', 'text', fetched_at=_NOW)

    report = _report(clean_db)
    assert report.articles == 2              # census: everything
    assert report.window_articles == 1       # flow: only what arrived in the window
    assert report.window_stamped == 1


# --- the empty branches, which is where the sibling report was broken -------------------------

def test_a_database_without_the_corpus_returns_an_empty_report_not_a_crash(clean_db):
    """The lesson from `breaking_report`'s UnboundLocalError, applied before it can repeat."""
    report = build_corpus_text_report(clean_db, _SINCE, since_label='7d',
                                      articles_table='articles_absent', keyword_sets=[_SET])
    assert report.articles == 0
    assert report.treatments == []
    assert report.phantoms == []
    assert '(the corpus is empty)' in format_corpus_text_report(report, width=100)


def test_an_empty_corpus_renders_rather_than_dividing_by_zero(clean_db, store):
    report = _report(clean_db)
    assert report.articles == 0
    assert '(the corpus is empty)' in format_corpus_text_report(report, width=100)
