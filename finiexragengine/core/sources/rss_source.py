"""RSS input source (feedparser-backed)."""
import urllib.request
from calendar import timegm
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

import feedparser

from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.exceptions.ragengine_errors import SourceFetchError
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.source_set_types import SourceConfig


# Identify honestly on every feed fetch. feedparser's default agent (and a bare urllib request)
# is blocked outright by some hosts — fxstreet and cryptoslate answer HTTP 403 to it. An
# identified, non-browser UA passes their bot filter *and* behaves better than a spoofed browser
# string (measured: cryptoslate rate-limits a fake Chrome UA with 429). The feed_doctor imports
# this same constant so its diagnosis mirrors the worker's real request byte-for-byte.
USER_AGENT = 'Mozilla/5.0 (compatible; FiniexRAGEngine/1.0; +https://github.com/dc-deal/FiniexRAGEngine)'


class _TimeoutHandler(urllib.request.BaseHandler):
    """Stamps a deadline onto every feed request (ISSUE_73).

    `feedparser.parse()` exposes no `timeout` and never passes one to urllib, so its socket
    inherits `socket.getdefaulttimeout()` — `None`, i.e. block forever. On 2026-08-01 that hung a
    worker inside `ssl.do_handshake()` for nine days against a host that accepted the TCP
    connection and then went silent.

    feedparser *does* forward extra handlers into `build_opener`, and `OpenerDirector.open()`
    assigns `req.timeout` **before** running the request processors — so a `*_request` processor
    is the one seam that can override it without giving up feedparser's conditional GET
    (etag/304), gzip and encoding handling. A private 5-line adapter with a single consumer:
    `feed_doctor` passes `timeout=` to `urlopen` directly and needs nothing from here.
    """

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds

    def http_request(self, req: urllib.request.Request) -> urllib.request.Request:
        req.timeout = self._timeout_seconds
        return req

    # HTTPS travels the same path; urllib dispatches the processor per scheme.
    https_request = http_request


class RssSource(AbstractSource):
    """Fetches and parses an RSS feed into Articles.

    Maps each feed entry to an Article (title + summary only — no full-text
    scraping), assigns the idempotent article_id from the entry guid/link
    (ISSUE_3), stamps fetched_at as real-time UTC, and carries the configured
    source weight onto every article (ISSUE_5).

    Continuous ingest (ISSUE_11): the source is long-lived on the ingest worker, so it keeps the
    feed's ETag / Last-Modified between polls and sends them back as a **conditional GET** — an
    unchanged feed answers `304 Not Modified` (no body), so polling every few seconds stays cheap
    *and* polite (the real ban risk is bandwidth, not request count). An optional per-source
    `poll_interval_seconds` lets a genuinely slow feed opt out of the fast loop; central-bank feeds
    are deliberately NOT slowed — 304 keeps them fast and polite.
    """

    def __init__(self, config: SourceConfig, default_timeout_seconds: int = 10) -> None:
        super().__init__(config)
        # Conditional-GET validators, remembered across polls (in-memory — a cold start just
        # re-pulls once). None until the first successful fetch.
        self._etag: Optional[str] = None
        self._modified: Optional[str] = None
        self._last_polled_at: Optional[datetime] = None
        # Effective fetch deadline (ISSUE_73): the per-source override wins, else the set's
        # default. Resolved once here so every poll uses the same value without re-deriving it.
        self._timeout_seconds: int = config.timeout_seconds or default_timeout_seconds

    def due_for_fetch(self) -> bool:
        # Per-source poll floor (ISSUE_11): a feed that ignores conditional GET (e.g. cryptoslate,
        # which 429s a fast loop) opts out until its own interval elapses. Measured from the last
        # attempt (`_last_polled_at`, set in fetch). Unset floor -> always due (our fast tempo).
        floor = self._config.poll_interval_seconds
        if floor is None or self._last_polled_at is None:
            return True
        return (datetime.now(timezone.utc) - self._last_polled_at).total_seconds() >= floor

    def fetch(self) -> List[Article]:
        now = datetime.now(timezone.utc)
        # Stamp the attempt time — the poll floor in `due_for_fetch` measures from here. The
        # Ingestor gates the floor before calling fetch, so a within-floor pass never reaches here.
        self._last_polled_at = now
        url = self._config.url
        # Active pull with conditional GET + status/transport handling (ISSUE_11): returns the
        # parsed feed on success, or None on 304 (unchanged, no body); a non-success HTTP status
        # or a malformed/unreachable feed raises a *typed* SourceFetchError for source-health.
        parsed = self._fetch_parsed(url)
        if parsed is None:
            return []   # 304 — unchanged since the last poll, no body transferred
        # Remember the validators for the next conditional GET (only when the server sent them).
        if getattr(parsed, 'etag', None):
            self._etag = parsed.etag
        if getattr(parsed, 'modified', None):
            self._modified = parsed.modified

        entries = getattr(parsed, 'entries', []) or []
        fetched_at = now
        feed_meta = getattr(parsed, 'feed', {}) or {}
        articles: List[Article] = []
        for entry in entries:
            link = (entry.get('link') or '').strip()
            guid = (entry.get('id') or '').strip() or None
            if not link and not guid:
                # No stable identity → cannot dedup; skip rather than poison the corpus.
                continue
            articles.append(
                Article(
                    article_id=Article.make_id(link, guid),
                    source_id=self.get_source_id(),
                    source_weight=self._config.weight,
                    url=link,
                    title=(entry.get('title') or '').strip(),
                    summary=self._summary(entry),
                    language=self._language(feed_meta, entry),
                    published_at=self._published_at(entry, fetched_at),
                    fetched_at=fetched_at,
                )
            )
        return articles

    def _fetch_parsed(self, url: str) -> Optional[feedparser.FeedParserDict]:
        """Conditional GET with typed failure classification (ISSUE_11).

        Returns the parsed feed on success, None on HTTP 304. Raises a typed SourceFetchError
        otherwise so source-health can classify without parsing the message:
        - a non-success HTTP status (the body is an error/hint page, NOT the feed) → RATE_LIMITED
          (429) or HTTP_ERROR — never parsed as XML (that is exactly the '429-HTML → not
          well-formed' trap);
        - a transport/TLS failure (feedparser never reached a body) → one retry, then UNREACHABLE;
        - a malformed body with no usable entries → PARSE_ERROR (a retry would not fix it).
        A bozo feed that still yielded entries is tolerated (feedparser is lenient).
        """
        for attempt in (1, 2):
            # agent set explicitly: without it feedparser sends its default UA, which some hosts
            # (fxstreet, cryptoslate) reject with 403 before the body is ever produced.
            # handlers: the fetch deadline (ISSUE_73) — without it a silent host blocks this
            # thread forever.
            #
            # The deadline can fire in two places, and feedparser only shields one of them
            # (observed in production 2026-08-11, five times):
            #   - while CONNECTING or sending — inside `h.request()`, which CPython's
            #     `AbstractHTTPHandler.do_open` wraps into `URLError`. feedparser catches that and
            #     reports it as `bozo_exception`, handled by the transient branch below.
            #   - while READING THE RESPONSE — `h.getresponse()` sits *outside* that `except
            #     OSError`, and the enclosing `except: raise` re-raises bare. So a raw
            #     `TimeoutError` escapes `feedparser.parse()` entirely.
            # Unhandled, the second case propagated past `Ingestor.run`'s per-source
            # `except SourceFetchError` and killed the whole pass — one slow feed taking every
            # other feed in the set down with it, and never reaching quarantine. Catching OSError
            # here routes both paths into the same typed, retried, quarantine-able failure.
            try:
                parsed = feedparser.parse(url, etag=self._etag, modified=self._modified,
                                          agent=USER_AGENT,
                                          handlers=[_TimeoutHandler(self._timeout_seconds)])
            except OSError as exc:            # TimeoutError/socket errors ⊂ OSError
                if attempt == 1:
                    continue                  # same one retry a transient bozo failure gets
                raise SourceFetchError(
                    f'{self.get_source_id()}: cannot fetch feed {url} ({exc})',
                    error_type='UNREACHABLE') from exc
            status = getattr(parsed, 'status', None)
            if status == 304:
                return None
            if status is not None and status >= 400:
                kind = 'RATE_LIMITED' if status == 429 else 'HTTP_ERROR'
                raise SourceFetchError(
                    f'{self.get_source_id()}: {url} returned HTTP {status}',
                    error_type=kind, status=status)
            entries = getattr(parsed, 'entries', []) or []
            if not (getattr(parsed, 'bozo', 0) and not entries):
                return parsed   # success: real entries, or a clean empty/valid feed
            exc = getattr(parsed, 'bozo_exception', None)
            transient = isinstance(exc, OSError)   # URLError/SSLError/Connection/Timeout ⊂ OSError
            if transient and attempt == 1:
                continue        # one retry for a transient TLS/transport drop (e.g. central banks)
            raise SourceFetchError(
                f'{self.get_source_id()}: cannot fetch feed {url} ({exc or "unknown error"})',
                error_type='UNREACHABLE' if transient else 'PARSE_ERROR')
        # The loop always returns or raises; this satisfies the type-checker.
        raise SourceFetchError(f'{self.get_source_id()}: cannot fetch feed {url}',
                               error_type='UNREACHABLE')

    def _summary(self, entry: Mapping[str, Any]) -> str:
        return (entry.get('summary') or entry.get('description') or '').strip()

    def _language(self, feed_meta: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
        return (entry.get('language') or feed_meta.get('language') or '').strip()

    def _published_at(self, entry: Mapping[str, Any], fallback: datetime) -> datetime:
        for key in ('published_parsed', 'updated_parsed'):
            parsed_time = entry.get(key)
            if parsed_time is not None:
                return datetime.fromtimestamp(timegm(parsed_time), tz=timezone.utc)
        return fallback
