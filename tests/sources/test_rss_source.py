"""Unit tests for RssSource.fetch — parsing, idempotent ids, provenance, errors, timeouts."""
import socket
import threading
import time
import urllib.request
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
        feedparser, 'parse', lambda url, etag=None, modified=None, agent=None, handlers=None: _FakeParsed(entries, feed={'language': 'en'})
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
    monkeypatch.setattr(feedparser, 'parse', lambda url, etag=None, modified=None, agent=None, handlers=None: _FakeParsed(entries))
    assert _source().fetch()[0].article_id == _source().fetch()[0].article_id


def test_fetch_skips_entries_without_identity(monkeypatch):
    entries = [{'title': 'no id', 'summary': 'x'}]
    monkeypatch.setattr(feedparser, 'parse', lambda url, etag=None, modified=None, agent=None, handlers=None: _FakeParsed(entries))
    assert _source().fetch() == []


def test_fetch_falls_back_to_fetched_at_when_no_pubdate(monkeypatch):
    entries = [{'id': 'g', 'link': 'https://example.test/a', 'title': 't', 'summary': 's'}]
    monkeypatch.setattr(feedparser, 'parse', lambda url, etag=None, modified=None, agent=None, handlers=None: _FakeParsed(entries))
    article = _source().fetch()[0]
    assert article.published_at == article.fetched_at


def test_fetch_raises_on_unreachable_feed(monkeypatch):
    monkeypatch.setattr(
        feedparser, 'parse', lambda url, etag=None, modified=None, agent=None, handlers=None: _FakeParsed([], bozo=1, bozo_exception='timeout')
    )
    with pytest.raises(SourceFetchError):
        _source().fetch()


def test_conditional_get_sends_etag_and_304_returns_empty(monkeypatch):
    # ISSUE_11: the source remembers the feed's ETag and sends it on the next poll; an
    # unchanged feed answers 304 with no body, so fast polling stays cheap + polite.
    seen_etags = []

    def fake_parse(url, etag=None, modified=None, agent=None, handlers=None):
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


def test_http_429_raises_rate_limited_without_parsing(monkeypatch):
    # ISSUE_11: a 429's body is an HTML error page, NOT the feed — classify it as RATE_LIMITED
    # from the status instead of choking on 'not well-formed' (the real cryptoslate bug).
    monkeypatch.setattr(feedparser, 'parse',
                        lambda url, etag=None, modified=None, agent=None, handlers=None: _FakeParsed([], bozo=1, status=429))
    with pytest.raises(SourceFetchError) as exc:
        _source().fetch()
    assert exc.value.error_type == 'RATE_LIMITED' and exc.value.status == 429


def test_http_5xx_raises_http_error(monkeypatch):
    monkeypatch.setattr(feedparser, 'parse',
                        lambda url, etag=None, modified=None, agent=None, handlers=None: _FakeParsed([], status=503))
    with pytest.raises(SourceFetchError) as exc:
        _source().fetch()
    assert exc.value.error_type == 'HTTP_ERROR' and exc.value.status == 503


def test_transport_error_retries_once_then_succeeds(monkeypatch):
    # A transient TLS/transport drop (OSError) is worth one retry — a central-bank feed with an
    # occasional SSL EOF should not be recorded as failing when the retry succeeds.
    calls = []
    good = [{'id': 'g', 'link': 'https://example.test/a', 'title': 't', 'summary': 's'}]

    def fake_parse(url, etag=None, modified=None, agent=None, handlers=None):
        calls.append(url)
        if len(calls) == 1:
            return _FakeParsed([], bozo=1, bozo_exception=OSError('SSL: UNEXPECTED_EOF'))
        return _FakeParsed(good)

    monkeypatch.setattr(feedparser, 'parse', fake_parse)
    assert len(_source().fetch()) == 1
    assert len(calls) == 2                               # failed once, retried, succeeded


def test_persistent_transport_error_raises_unreachable(monkeypatch):
    monkeypatch.setattr(feedparser, 'parse', lambda url, etag=None, modified=None, agent=None, handlers=None:
                        _FakeParsed([], bozo=1, bozo_exception=OSError('conn refused')))
    with pytest.raises(SourceFetchError) as exc:
        _source().fetch()
    assert exc.value.error_type == 'UNREACHABLE'


def test_malformed_body_raises_parse_error_without_retry(monkeypatch):
    # A non-transport bozo (malformed XML) will not fix itself — classify PARSE_ERROR, no retry.
    calls = []

    def fake_parse(url, etag=None, modified=None, agent=None, handlers=None):
        calls.append(url)
        return _FakeParsed([], bozo=1, bozo_exception=ValueError('not well-formed'))

    monkeypatch.setattr(feedparser, 'parse', fake_parse)
    with pytest.raises(SourceFetchError) as exc:
        _source().fetch()
    assert exc.value.error_type == 'PARSE_ERROR'
    assert len(calls) == 1                               # no retry on a malformed body


def test_poll_interval_floor_gates_due_for_fetch(monkeypatch):
    # A slow feed opts out of the fast loop via due_for_fetch (the Ingestor gates on it so a
    # floor skip is a local no-op, never a recorded poll). Within its interval it is not due.
    monkeypatch.setattr(feedparser, 'parse', lambda url, etag=None, modified=None, agent=None, handlers=None: _FakeParsed(
        [{'id': 'g', 'link': 'https://example.test/a', 'title': 't', 'summary': 's'}]))
    source = RssSource(SourceConfig(source_id='slow', url='https://example.test/rss',
                                    poll_interval_seconds=3600))
    assert source.due_for_fetch() is True      # never polled -> due
    source.fetch()                             # stamps the attempt time
    assert source.due_for_fetch() is False     # within the floor -> not due


def test_no_floor_is_always_due():
    source = RssSource(SourceConfig(source_id='fast', url='https://example.test/rss'))
    assert source.due_for_fetch() is True      # no poll_interval_seconds -> our fast tempo


# --- ISSUE_73: the fetch deadline -----------------------------------------------------------


def test_per_source_timeout_overrides_the_set_default():
    source = RssSource(SourceConfig(source_id='slow_but_alive', url='https://example.test/rss',
                                    timeout_seconds=25), default_timeout_seconds=10)
    assert source._timeout_seconds == 25


def test_set_default_applies_without_a_per_source_override():
    source = RssSource(SourceConfig(source_id='normal', url='https://example.test/rss'),
                       default_timeout_seconds=10)
    assert source._timeout_seconds == 10


def test_the_timeout_reaches_the_request(monkeypatch):
    # The whole point of ISSUE_73: feedparser takes no `timeout=`, so the deadline can only ride
    # in on a handler. Assert it is actually handed over AND that it stamps a real Request —
    # a handler that never runs would look identical from the outside.
    seen = {}

    def fake_parse(url, etag=None, modified=None, agent=None, handlers=None):
        seen['handlers'] = handlers
        return _FakeParsed([{'id': 'g', 'link': 'https://example.test/a',
                             'title': 't', 'summary': 's'}])

    monkeypatch.setattr(feedparser, 'parse', fake_parse)
    RssSource(SourceConfig(source_id='x', url='https://example.test/rss'),
              default_timeout_seconds=7).fetch()

    handler = seen['handlers'][0]
    request = urllib.request.Request('https://example.test/rss')
    assert handler.https_request(request).timeout == 7
    assert handler.http_request(request).timeout == 7


def test_a_host_that_never_answers_is_classified_not_propagated():
    """ISSUE_73 follow-up — the gap production found on 2026-08-11 (five times).

    The deadline can fire in two places. A timeout while *connecting* is wrapped into `URLError`
    by CPython and caught by feedparser. A timeout while *reading the response* is not: it escapes
    `feedparser.parse()` as a bare `TimeoutError`, sailed past `Ingestor.run`'s per-source
    `except SourceFetchError`, and killed the entire ingest pass — every other feed in the set
    with it, and never reaching quarantine.

    This server accepts the connection and reads the request, then answers nothing: the second
    path exactly. It must come out as a typed `UNREACHABLE`, like any other unreachable feed.
    """
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0))
    port = server.getsockname()[1]
    server.listen(2)

    def stall() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.recv(4096)                    # take the request …
            time.sleep(5)                      # … and never answer it

    threading.Thread(target=stall, daemon=True).start()
    try:
        source = RssSource(SourceConfig(source_id='silent',
                                        url=f'http://127.0.0.1:{port}/feed.xml',
                                        timeout_seconds=1))
        with pytest.raises(SourceFetchError) as exc:
            source.fetch()
        assert exc.value.error_type == 'UNREACHABLE'
        assert isinstance(exc.value.__cause__, OSError)   # the timeout is preserved as the cause
    finally:
        server.close()


def test_a_stalled_host_times_out_instead_of_hanging():
    # The 2026-08-01 regression, end to end and for real: a non-routable address blackholes the
    # connect, which without a timeout blocks forever. With one it must raise promptly — and be
    # classified UNREACHABLE so source-health can quarantine the feed (5 strikes, 24h).
    source = RssSource(SourceConfig(source_id='blackhole',
                                    url='http://10.255.255.1/feed.xml',
                                    timeout_seconds=1))
    started = time.monotonic()
    with pytest.raises(SourceFetchError) as exc:
        source.fetch()
    elapsed = time.monotonic() - started
    assert exc.value.error_type == 'UNREACHABLE'
    assert elapsed < 10          # two attempts x 1s deadline, generous slack for slow CI
