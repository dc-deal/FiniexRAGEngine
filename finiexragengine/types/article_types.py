"""Runtime domain type for an ingested news article."""
import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Article:
    """A single ingested news article (raw, source-agnostic).

    Args:
        article_id: Idempotent identity key (hash of guid/url) — dedup across feeds and polls.
        source_id: Originating source identifier.
        source_weight: Trust / weight of the source (from the constellation config).
        url: Canonical article URL.
        title: Article headline.
        summary: Short summary / excerpt (full-text scraping is out of scope).
        language: Best-effort ISO language code.
        published_at: Publication time as reported by the feed (UTC, tz-aware).
        fetched_at: Time the article was fetched into the engine (UTC, tz-aware).
    """
    article_id: str
    source_id: str
    source_weight: float
    url: str
    title: str
    summary: str
    language: str
    published_at: datetime
    fetched_at: datetime

    @staticmethod
    def make_id(url: str, guid: str | None = None) -> str:
        """Build the idempotent identity key from the article's guid/url.

        Args:
            url: Canonical article URL.
            guid: Feed-provided GUID, if any (preferred when present).

        Returns:
            A stable hex digest used as the dedup key (ISSUE_3).
        """
        basis = (guid or url).strip().lower()
        return hashlib.sha256(basis.encode('utf-8')).hexdigest()[:32]


@dataclass
class ScoredArticle:
    """A vector-store match: the article plus its retrieval-time score context (ISSUE_5).

    Args:
        article: The matched article.
        distance: Cosine distance to the query vector (lower = more similar).
        embedding: The stored embedding — lets retrieval collapse near-duplicate
            stories pairwise without re-embedding.
        importance: Corpus importance tag (None until the breaking detector sets it).
    """
    article: Article
    distance: float
    embedding: list[float]
    importance: int | None = None
