"""Retrieval stage — the RAG 'squeeze': only relevant, recent, deduped context."""
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.types.article_types import (
    RetrievalTier,
    RetrievedArticle,
    RetrievedContext,
    ScoredArticle,
)
from finiexragengine.types.config_types.pipeline_config_types import RetrievalConfig
from finiexragengine.types.outcome_types import RetrievalFunnel

RECENT: RetrievalTier = 'recent'
DEEP: RetrievalTier = 'deep'
# Ordering rank per tier — NOT the label, and the distinction is load-bearing. The tier used to be
# the integers 0/1 and `_rank_key` sorted on them directly; sorting the labels instead would order
# them alphabetically, put 'deep' ahead of 'recent', and rank week-old articles above today's news.
# That is the precise contamination ISSUE_30 exists to prevent, and it would be silent.
_TIER_RANK = {RECENT: 0, DEEP: 1}


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity of two vectors (0.0 when either has zero norm)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _rank_key(item: Tuple[RetrievalTier, ScoredArticle]) -> Tuple:
    """Ordering: tier (recency dominates) → distance → source_weight → importance."""
    tier, hit = item
    importance = hit.importance if hit.importance is not None else -1
    return (_TIER_RANK[tier], round(hit.distance, 4), -hit.article.source_weight, -importance)


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

    def retrieve(self, query: str) -> RetrievedContext:
        """Return the relevant, recent, deduped context for `query` — plus its funnel.

        Args:
            query: Query text (e.g. from SymbolQueryMap.query_for).

        Returns:
            At most `top_k` articles (best candidate first) and the funnel counters of
            how the squeeze arrived there (ISSUE_24) — so an empty context is
            explainable: was the window empty, or did the floor drop everything?
        """
        vector = self._query_cache.get_vector(query)   # cached — embeds once, then reused (ISSUE_19)
        now = datetime.now(timezone.utc)
        fetch_k = self._config.top_k * self._OVERFETCH
        recent_since = now - timedelta(minutes=self._config.recency_window_minutes)
        candidates = [(RECENT, hit) for hit in self._store.query(vector, fetch_k, recent_since)]
        deep = self._config.deep_tier
        if deep is not None:
            deep_since = now - timedelta(minutes=deep.window_minutes)
            candidates += [(DEEP, hit) for hit in self._store.query(
                vector, fetch_k, deep_since, min_importance=deep.min_importance)]
        # Funnel capture starts here: everything the windows offered, and the distance
        # spread *before* the floor — best doubles as the "nearest miss" when the floor
        # empties the set; together with the applied floor it places the cut in the spread.
        in_window = len(candidates)
        best_distance = min((hit.distance for _tier, hit in candidates), default=None)
        worst_distance = max((hit.distance for _tier, hit in candidates), default=None)
        # Relevance floor (ISSUE_24), before dedup: an off-topic candidate must never
        # reach the prompt, and dropping it here also spares the pairwise dedup work.
        # An empty survivor set is a *result* — the evaluator answers it with the
        # mechanical no_data HOLD instead of paying for an LLM read of generic articles.
        floor = self._config.floor_distance
        if floor is not None:
            candidates = [(tier, hit) for tier, hit in candidates if hit.distance <= floor]
        floor_dropped = in_window - len(candidates)
        candidates.sort(key=_rank_key)
        retrieved, tier_duplicates, near_duplicates = self._squeeze(candidates)
        # Derived from the list rather than accumulated beside it (ISSUE_30): one computation, so
        # the funnel's count and the per-citation tiers in the envelope cannot disagree. They used
        # to be two, and two numbers meant to agree are the shape ISSUE_82 spent weeks on.
        deep_kept = sum(1 for item in retrieved if item.retrieval_tier == DEEP)
        return RetrievedContext(retrieved=retrieved, funnel=RetrievalFunnel(
            in_window=in_window, floor_dropped=floor_dropped,
            tier_duplicates=tier_duplicates, near_duplicates=near_duplicates,
            kept=len(retrieved), deep_kept=deep_kept, best_distance=best_distance,
            worst_distance=worst_distance, floor=floor))

    def _squeeze(self, ranked: List[Tuple[RetrievalTier, ScoredArticle]]
                 ) -> Tuple[List[RetrievedArticle], int, int]:
        """Collapse id- and near-duplicates in rank order and cap at top_k.

        Returns the kept articles **paired with the tier that surfaced them**, plus the two
        collapse counters (tier duplicates, near-duplicates). The tier was always available in this
        loop and was discarded one line later (ISSUE_30); keeping it is what lets the prompt fence
        a retrospective article and the envelope record which citations were retrospective.
        """
        kept: List[Tuple[RetrievalTier, ScoredArticle]] = []
        seen_ids = set()
        tier_duplicates = 0
        near_duplicates = 0
        for tier, hit in ranked:
            if hit.article.article_id in seen_ids:
                tier_duplicates += 1
                continue   # same article surfaced by both tiers
            if any(_cosine(hit.embedding, other.embedding) >= self._config.dedup_similarity
                   for _tier, other in kept):
                near_duplicates += 1
                continue   # near-duplicate story from another feed
            seen_ids.add(hit.article.article_id)
            kept.append((tier, hit))
            if len(kept) == self._config.top_k:
                break   # cap applied after dedup, so duplicates never consume a slot
        return ([RetrievedArticle(article=hit.article, retrieval_tier=tier)
                 for tier, hit in kept], tier_duplicates, near_duplicates)
