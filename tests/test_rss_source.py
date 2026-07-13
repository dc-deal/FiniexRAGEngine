"""Unit tests for RssSource.fetch — parsing, idempotent ids, provenance, errors."""
import time
from datetime import timezone

import feedparser
import pytest

from finiexragengine.core.sources.rss_source import RssSource
from finiexragengine.exceptions.ragengine_errors import SourceFetchError
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.source_set_types import SourceConfig


class _FakeParsed:
    """Minimal stand-in for a feedparser result (attribute access + dict entries)."""

    def __init__(self, entries, bozo=0, bozo_exception=None, feed=None,
                 status=None, etag=None, modified=None):
        self.entries = entries
        self.bozo = bozo
        self.bozo_exception = bozo_exception
        self.feed = feed or {}
        # Conditional-GET fields (ISSUE_11): only set when the test needs them.
        if status is not None:
            self.status = status
        if etag is not None:
            self.etag = etag
        if modified is not None:
            self.modified = modified


def _source(weight: float = 0.8) -> RssSource:
    return RssSource(SourceConfig(
        source_id='cointelegraph',
        type='rss',
        url='https://example.test/rss',
        weight=weight,
    ))


def test_fetch_maps_entries_to_articles(monkeypatch):
    published = time.struct_time((2026, 6, 28, 12, 0, 0, 0, 0, 0))
    entries = [{
        'id': 'guid-1',
        'link': 'https://example.test/a',
        'title': 'BTC rallies',
        'summary': 'Bitcoin up.',
        'published_parsed': published,
    }]
    monkeypatch.setattr(
        feedparser, 'parse', lambda url, etag=None, modified=None: _FakeParsed(entries, feed={'language': 'en'})
    )

    articles = _source().fetch()

    assert len(articles) == 1
    article = articles[0]
    assert article.article_id == Article.make_id('https://example.test/a', 'guid-1')
    assert article.source_id == 'cointelegraph'
    assert article.source_weight == 0.8
    assert article.url == 'https://example.test/a'
    assert article.title == 'BTC rallies'
    assert article.summary == 'Bitcoin up.'
    assert article.language == 'en'
    assert article.published_at.tzinfo is not None
    assert article.published_at.year == 2026
    assert article.fetched_at.tzinfo == timezone.utc


def test_fetch_is_idempotent_on_id(monkeypatch):
    entries = [{'id': 'guid-1', 'link': 'https://example.test/a', 'title': 't', 'summary': 's'}]
    monkeypatch.setattr(feedparser, 'parse', lambda url, etag=None, modified=None: _FakeParsed(entries))
    assert _source().fetch()[0].article_id == _source().fetch()[0].article_id


def test_fetch_skips_entries_without_identity(monkeypatch):
    entries = [{'title': 'no id', 'summary': 'x'}]
    monkeypatch.setattr(feedparser, 'parse', lambda url, etag=None, modified=None: _FakeParsed(entries))
    assert _source().fetch() == []


def test_fetch_falls_back_to_fetched_at_when_no_pubdate(monkeypatch):
    entries = [{'id': 'g', 'link': 'https://example.test/a', 'title': 't', 'summary': 's'}]
    monkeypatch.setattr(feedparser, 'parse', lambda url, etag=None, modified=None: _FakeParsed(entries))
    article = _source().fetch()[0]
    assert article.published_at == article.fetched_at


def test_fetch_raises_on_unreachable_feed(monkeypatch):
    monkeypatch.setattr(
        feedparser, 'parse', lambda url, etag=None, modified=None: _FakeParsed([], bozo=1, bozo_exception='timeout')
    )
    with pytest.raises(SourceFetchError):
        _source().fetch()


def test_conditional_get_sends_etag_and_304_returns_empty(monkeypatch):
    # ISSUE_11: the source remembers the feed's ETag and sends it on the next poll; an
    # unchanged feed answers 304 with no body, so fast polling stays cheap + polite.
    seen_etags = []

    def fake_parse(url, etag=None, modified=None):
        seen_etags.append(etag)
        if etag is None:
            return _FakeParsed(
                [{'id': 'g', 'link': 'https://example.test/a', 'title': 't', 'summary': 's'}],
                etag='"v1"')
        return _FakeParsed([], status=304)     # unchanged since the stored validator

    monkeypatch.setattr(feedparser, 'parse', fake_parse)
    source = _source()
    first = source.fetch()
    second = source.fetch()
    assert len(first) == 1
    assert second == []                        # 304 -> no new articles, no body transferred
    assert seen_etags == [None, '"v1"']        # the stored ETag was sent on the second poll


def test_poll_interval_floor_skips_within_the_window(monkeypatch):
    # A slow feed opts out of the fast loop: within its poll_interval it is not hit at all.
    parses = []

    def fake_parse(url, etag=None, modified=None):
        parses.append(url)
        return _FakeParsed(
            [{'id': 'g', 'link': 'https://example.test/a', 'title': 't', 'summary': 's'}])

    monkeypatch.setattr(feedparser, 'parse', fake_parse)
    source = RssSource(SourceConfig(source_id='slow', url='https://example.test/rss',
                                    poll_interval_seconds=3600))
    source.fetch()                             # first poll goes through
    assert source.fetch() == []                # within the floor -> skipped
    assert len(parses) == 1                    # the feed was hit exactly once
