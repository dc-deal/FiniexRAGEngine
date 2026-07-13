"""RSS input source (feedparser-backed)."""
from calendar import timegm
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional

import feedparser

from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.exceptions.ragengine_errors import SourceFetchError
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.source_set_types import SourceConfig


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

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        # Conditional-GET validators, remembered across polls (in-memory — a cold start just
        # re-pulls once). None until the first successful fetch.
        self._etag: Optional[str] = None
        self._modified: Optional[str] = None
        self._last_polled_at: Optional[datetime] = None

    def fetch(self) -> List[Article]:
        now = datetime.now(timezone.utc)
        # Per-source poll floor: a slow feed skips the fast loop until its own interval elapses.
        floor = self._config.poll_interval_seconds
        if (floor is not None and self._last_polled_at is not None
                and (now - self._last_polled_at).total_seconds() < floor):
            return []
        self._last_polled_at = now

        url = self._config.url
        # Active pull with conditional GET (ISSUE_11): send the last ETag / Last-Modified so an
        # unchanged feed answers 304 with no body. Nothing is pushed to us — the only push path
        # in the system is the separate live breaking channel (ISSUE_11 Stage C).
        parsed = feedparser.parse(url, etag=self._etag, modified=self._modified)
        if getattr(parsed, 'status', None) == 304:
            return []   # unchanged since the last poll — no new articles, no body transferred
        # Remember the validators for the next conditional GET (only when the server sent them).
        if getattr(parsed, 'etag', None):
            self._etag = parsed.etag
        if getattr(parsed, 'modified', None):
            self._modified = parsed.modified

        entries = getattr(parsed, 'entries', []) or []
        # feedparser sets bozo on a malformed feed; a transport failure surfaces
        # as bozo with no usable entries. Empty-but-valid feeds are not an error.
        if getattr(parsed, 'bozo', 0) and not entries:
            reason = getattr(parsed, 'bozo_exception', 'unknown error')
            raise SourceFetchError(
                f'{self.get_source_id()}: cannot fetch feed {url} ({reason})'
            )

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
