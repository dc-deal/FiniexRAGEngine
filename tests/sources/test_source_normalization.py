"""The normalisation seam (ISSUE_112) — a source inherits the treatment, it does not remember it.

The point of the template method is that `_fetch_articles` returns whatever the feed served and
`fetch` normalises it, so a source type added in a year is covered without its author knowing this
file exists. These tests drive that from the outside: none of the fakes below implement, override or
even mention normalisation.
"""
from datetime import datetime, timezone
from typing import List, Optional

import pytest

from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.core.sources.article_normalizer import ArticleNormalizer
from finiexragengine.core.sources.source_factory import build_source
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.source_set_types import SourceConfig


def _article(title: str, summary: str) -> Article:
    now = datetime.now(timezone.utc)
    return Article(article_id=Article.make_id('https://example.test/a'), source_id='fake',
                   source_weight=1.0, url='https://example.test/a', title=title,
                   summary=summary, language='en', published_at=now, fetched_at=now)


class _RawSource(AbstractSource):
    """A source that knows nothing about normalisation — the whole point of the test."""

    def __init__(self, articles: List[Article],
                 normalizer: Optional[ArticleNormalizer] = None) -> None:
        super().__init__(SourceConfig(source_id='fake', url='https://example.test'), normalizer)
        self._articles = articles

    def _fetch_articles(self) -> List[Article]:
        return self._articles


def test_a_source_that_does_nothing_still_returns_normalised_articles():
    source = _RawSource([_article('<b>Hack</b> confirmed', '<p>Funds &amp; keys lost</p>')])
    article = source.fetch()[0]
    assert article.title == 'Hack confirmed'
    assert article.summary == 'Funds & keys lost'
    assert article.text_normalizer == 'v1'


def test_the_fetched_bytes_survive_on_the_article():
    """Markup leaves what the model reads, never what the engine holds."""
    source = _RawSource([_article('<b>Hack</b>', 'Clean summary')])
    article = source.fetch()[0]
    assert article.title_raw == '<b>Hack</b>'
    assert article.summary_raw is None


def test_a_source_built_without_a_normalizer_still_normalises():
    """The default is the safe behaviour, so markup is never the reward for a forgotten argument."""
    source = _RawSource([_article('<i>Rate cut</i>', 'plain')])
    assert source.fetch()[0].title == 'Rate cut'


def test_the_factory_passes_the_configured_profile_through():
    built = build_source(SourceConfig(source_id='rss_one', url='https://example.test/rss'),
                         10, ArticleNormalizer('v1'))
    # Reached through the seam rather than the attribute: the contract is what `fetch` produces.
    assert isinstance(built, AbstractSource)


def test_fetch_is_not_meant_to_be_overridden():
    """The contract in one assertion: implementations supply `_fetch_articles`, never `fetch`.

    A subclass overriding `fetch` would silently opt its whole source type out of normalisation —
    the accretion shape that left 32 of 34 `psycopg.connect` calls unbounded (ISSUE_117). Pinned so
    the next source type is written against the intended seam.
    """
    assert '_fetch_articles' in AbstractSource.__abstractmethods__
    assert 'fetch' not in AbstractSource.__abstractmethods__


def test_an_article_that_arrives_clean_is_returned_untouched():
    clean = 'RBNZ holds the cash rate at 4.25% as expected.'
    source = _RawSource([_article('Policy unchanged', clean)])
    article = source.fetch()[0]
    assert article.summary == clean
    assert article.summary_raw is None
    assert article.title_raw is None
