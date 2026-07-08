"""Abstract base for the vector store backing the RAG layer."""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Set

from finiexragengine.types.article_types import Article, ScoredArticle


class AbstractVectorStore(ABC):
    """Persists embedded articles and answers similarity queries.

    The store is the growing news corpus. It must be idempotent on
    Article.article_id (ISSUE_3) and retain raw articles + timestamps so the
    corpus can be re-analyzed later (backfill / replay, ISSUE_4).
    """

    @abstractmethod
    def upsert(self, articles: List[Article], vectors: List[List[float]]) -> int:
        """Insert/update articles by article_id (idempotent).

        Args:
            articles: The articles to persist.
            vectors: One embedding per article, order-aligned with `articles`.

        Returns:
            Number of rows actually written (duplicates skipped).
        """
        ...

    @abstractmethod
    def query(self, vector: List[float], top_k: int, since: datetime,
              min_importance: Optional[int] = None) -> List[ScoredArticle]:
        """Return the most similar articles published at/after `since`.

        Args:
            vector: The query embedding.
            top_k: Maximum number of articles to return.
            since: Recency lower bound — stale-but-similar articles below this
                are excluded so they do not dominate a current-state query (ISSUE_3).
            min_importance: If set, restrict to articles with `importance >= this`
                (the deep/old tier of the two-tier retrieval policy, ISSUE_5).

        Returns:
            The matches as ScoredArticle (cosine distance + stored embedding +
            importance tag), most similar first.
        """
        ...

    @abstractmethod
    def existing_ids(self, article_ids: List[str]) -> Set[str]:
        """Return the subset of article_ids already stored.

        Lets the ingest side skip articles it already holds: only the upsert is
        idempotent, but *embedding* a known article again is wasted API spend, so
        ingest checks existence first and embeds only the genuinely new ids.
        """
        ...
