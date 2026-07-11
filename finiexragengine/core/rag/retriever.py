"""Retrieval stage — the RAG 'squeeze': only relevant, recent, deduped context."""
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.types.article_types import Article, ScoredArticle
from finiexragengine.types.config_types.pipeline_config_types import RetrievalConfig


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity of two vectors (0.0 when either has zero norm)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _rank_key(item: Tuple[int, ScoredArticle]) -> Tuple:
    """Ordering: tier (recency dominates) → distance → source_weight → importance."""
    tier, hit = item
    importance = hit.importance if hit.importance is not None else -1
    return (tier, round(hit.distance, 4), -hit.article.source_weight, -importance)


class Retriever:
    """Selects the relevant article context for a query (e.g. a symbol).

    This is where the token budget is solved (ISSUE_5): resolve the query vector
    (cached — ISSUE_19), pull the most-similar candidates from the store, then
    squeeze them down:

    - *recent tier* — candidates inside `recency_window_minutes` (broad);
    - *deep tier* (opt-in via `retrieval.deep_tier`) — older articles enter
      only when their `importance` reaches `min_importance`. Recency dominates:
      deep candidates always rank behind recent ones;
    - within a tier: ascending cosine distance; on distance ties a higher
      `source_weight`, then a higher `importance` wins;
    - near-duplicate stories across feeds are collapsed via pairwise cosine on
      the stored embeddings (>= `dedup_similarity`), keeping the better-ranked;
    - `top_k` is the hard cap on what reaches the prompt.
    """

    _OVERFETCH = 2   # pull extra candidates per tier so dedup cannot starve top_k

    def __init__(self, query_cache: QueryVectorCache, store: AbstractVectorStore,
                 config: RetrievalConfig) -> None:
        self._query_cache = query_cache
        self._store = store
        self._config = config

    def retrieve(self, query: str) -> List[Article]:
        """Return the relevant, recent, deduped context for `query`.

        Args:
            query: Query text (e.g. from SymbolQueryMap.query_for).

        Returns:
            At most `top_k` articles, best candidate first.
        """
        vector = self._query_cache.get_vector(query)   # cached — embeds once, then reused (ISSUE_19)
        now = datetime.now(timezone.utc)
        fetch_k = self._config.top_k * self._OVERFETCH
        recent_since = now - timedelta(minutes=self._config.recency_window_minutes)
        candidates = [(0, hit) for hit in self._store.query(vector, fetch_k, recent_since)]
        deep = self._config.deep_tier
        if deep is not None:
            deep_since = now - timedelta(minutes=deep.window_minutes)
            candidates += [(1, hit) for hit in self._store.query(
                vector, fetch_k, deep_since, min_importance=deep.min_importance)]
        # Relevance floor (ISSUE_24), before dedup: an off-topic candidate must never
        # reach the prompt, and dropping it here also spares the pairwise dedup work.
        # An empty survivor set is a *result* — the evaluator answers it with the
        # mechanical no_data HOLD instead of paying for an LLM read of generic articles.
        floor = self._config.floor_distance
        if floor is not None:
            candidates = [(tier, hit) for tier, hit in candidates if hit.distance <= floor]
        candidates.sort(key=_rank_key)
        return self._squeeze(candidates)

    def _squeeze(self, ranked: List[Tuple[int, ScoredArticle]]) -> List[Article]:
        """Collapse id- and near-duplicates in rank order and cap at top_k."""
        kept: List[ScoredArticle] = []
        seen_ids = set()
        for _tier, hit in ranked:
            if hit.article.article_id in seen_ids:
                continue   # same article surfaced by both tiers
            if any(_cosine(hit.embedding, other.embedding) >= self._config.dedup_similarity
                   for other in kept):
                continue   # near-duplicate story from another feed
            seen_ids.add(hit.article.article_id)
            kept.append(hit)
            if len(kept) == self._config.top_k:
                break   # cap applied after dedup, so duplicates never consume a slot
        return [hit.article for hit in kept]
