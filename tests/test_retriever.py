"""Unit tests for Retriever — recency, top_k cap, dedup, deep tier, tie-breaks.

Embedder and store are faked, so these run offline. The fake store records the
query arguments (window, min_importance) and returns pre-built ScoredArticle
hits with controlled distances, weights and embeddings.
"""
import itertools
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.rag.retriever import Retriever
from finiexragengine.types.article_types import Article, ScoredArticle
from finiexragengine.types.config_types.pipeline_config_types import (
    DeepTierConfig,
    RetrievalConfig,
)

_TS = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
_DIMS = 64
_axis = itertools.count(1)   # axis 0 is the query vector


def _unit(index: int) -> List[float]:
    vector = [0.0] * _DIMS
    vector[index % _DIMS] = 1.0
    return vector


class _FakeQueryVectorCache:
    """Returns a fixed query vector (axis 0) and records the queries asked for."""

    def __init__(self) -> None:
        self.queries: List[str] = []

    def get_vector(self, query_text: str) -> List[float]:
        self.queries.append(query_text)
        return _unit(0)


class _FakeStore(AbstractVectorStore):
    """Pops one prepared response per query call and records the arguments."""

    def __init__(self, responses: List[List[ScoredArticle]]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []

    def upsert(self, articles: List[Article], vectors: List[List[float]]) -> int:
        raise AssertionError('retrieval must not upsert')

    def existing_ids(self, article_ids: List[str]) -> set:
        raise AssertionError('retrieval must not check existence')

    def query(self, vector, top_k, since, min_importance=None):
        self.calls.append({'top_k': top_k, 'since': since,
                           'min_importance': min_importance})
        return self._responses.pop(0) if self._responses else []


def _hit(article_id: str, distance: float, weight: float = 1.0,
         importance: Optional[int] = None,
         embedding: Optional[List[float]] = None) -> ScoredArticle:
    article = Article(
        article_id=article_id, source_id='s', source_weight=weight,
        url=f'https://example.test/{article_id}', title=f'title-{article_id}',
        summary='summary', language='en', published_at=_TS, fetched_at=_TS)
    return ScoredArticle(article=article, distance=distance,
                         embedding=embedding or _unit(next(_axis)),
                         importance=importance)


def _retriever(store: _FakeStore, **kwargs) -> Retriever:
    return Retriever(_FakeQueryVectorCache(), store, RetrievalConfig(**kwargs))


def test_recent_tier_window_and_overfetch():
    store = _FakeStore([[_hit('a', 0.1)]])
    retriever = _retriever(store, top_k=3, recency_window_minutes=60)
    before = datetime.now(timezone.utc)
    result = retriever.retrieve('query text')
    after = datetime.now(timezone.utc)
    assert [a.article_id for a in result] == ['a']
    assert len(store.calls) == 1                      # deep tier off by default
    call = store.calls[0]
    assert call['top_k'] == 6                         # top_k * overfetch headroom
    assert call['min_importance'] is None
    assert before - timedelta(minutes=60) <= call['since'] <= after - timedelta(minutes=60)


def test_top_k_is_a_hard_cap():
    hits = [_hit(f'a{i}', 0.1 * i) for i in range(6)]
    store = _FakeStore([hits])
    result = _retriever(store, top_k=2).retrieve('q')
    assert [a.article_id for a in result] == ['a0', 'a1']


def test_orders_by_distance_within_tier():
    store = _FakeStore([[_hit('far', 0.7), _hit('near', 0.1), _hit('mid', 0.4)]])
    result = _retriever(store, top_k=10).retrieve('q')
    assert [a.article_id for a in result] == ['near', 'mid', 'far']


def test_distance_tie_breaks_on_source_weight_then_importance():
    store = _FakeStore([[
        _hit('light', 0.2, weight=0.5, importance=3),
        _hit('heavy', 0.2, weight=1.0),
        _hit('untagged', 0.2, weight=0.5),
    ]])
    result = _retriever(store, top_k=10).retrieve('q')
    assert [a.article_id for a in result] == ['heavy', 'light', 'untagged']


def test_near_duplicates_collapse_keeps_better_ranked():
    shared = _unit(1)
    related = [0.0] * _DIMS
    related[1] = 0.7071          # cosine ≈ 0.71 to `shared` — similar but no duplicate
    related[2] = 0.7071
    store = _FakeStore([[
        _hit('original', 0.1, embedding=list(shared)),
        _hit('syndicated', 0.2, embedding=list(shared)),
        _hit('related', 0.3, embedding=related),
    ]])
    result = _retriever(store, top_k=10).retrieve('q')
    assert [a.article_id for a in result] == ['original', 'related']


def test_deep_tier_opt_in_queries_and_ranks_behind_recent():
    recent = [_hit('recent', 0.5)]
    deep = [_hit('deep', 0.01, importance=3)]
    store = _FakeStore([recent, deep])
    retriever = _retriever(store, top_k=5, recency_window_minutes=60,
                           deep_tier=DeepTierConfig(min_importance=2, window_minutes=2880))
    result = retriever.retrieve('q')
    assert [a.article_id for a in result] == ['recent', 'deep']   # recency dominates
    assert len(store.calls) == 2
    assert store.calls[1]['min_importance'] == 2
    assert store.calls[1]['since'] < store.calls[0]['since']      # deep window reaches back further


def test_deep_tier_does_not_duplicate_recent_articles():
    store = _FakeStore([
        [_hit('both-tiers', 0.2)],
        [_hit('both-tiers', 0.2, importance=3)],
    ])
    retriever = _retriever(store, top_k=5, deep_tier=DeepTierConfig())
    result = retriever.retrieve('q')
    assert [a.article_id for a in result] == ['both-tiers']


def test_empty_store_yields_empty_context():
    store = _FakeStore([[]])
    assert _retriever(store, top_k=5).retrieve('q') == []
