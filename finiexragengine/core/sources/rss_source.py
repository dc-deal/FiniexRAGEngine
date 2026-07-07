"""RSS input source (feedparser-backed)."""
from calendar import timegm
from datetime import datetime, timezone
from typing import Any, List, Mapping

import feedparser

from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.exceptions.ragengine_errors import SourceFetchError
from finiexragengine.types.article_types import Article


class RssSource(AbstractSource):
    """Fetches and parses an RSS feed into Articles.

    Maps each feed entry to an Article (title + summary only — no full-text
    scraping), assigns the idempotent article_id from the entry guid/link
    (ISSUE_3), stamps fetched_at as real-time UTC, and carries the configured
    source weight onto every article (ISSUE_5).
    """

    def fetch(self) -> List[Article]:
        url = self._config.url
        parsed = feedparser.parse(url)
        entries = getattr(parsed, 'entries', []) or []
        # feedparser sets bozo on a malformed feed; a transport failure surfaces
        # as bozo with no usable entries. Empty-but-valid feeds are not an error.
        if getattr(parsed, 'bozo', 0) and not entries:
            reason = getattr(parsed, 'bozo_exception', 'unknown error')
            raise SourceFetchError(
                f'{self.get_source_id()}: cannot fetch feed {url} ({reason})'
            )

        fetched_at = datetime.now(timezone.utc)
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
