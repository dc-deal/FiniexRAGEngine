"""Integration tests for QueryVectorCache — hit skips embed, text/model = new key.

Skipped when psycopg/pgvector or a reachable PostgreSQL is missing. The embedder is faked
(call-counting), so these spend no API budget — only the cache/DB logic is tested. Runs against
the canonical `query_vectors` table in the isolated, migration-built test schema (`clean_db`,
ISSUE_14), at the real 1536 width the column declares.
"""
from typing import Callable, List

import pytest

from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache

_DIMS = 1536


class _CountingEmbedder(AbstractEmbedder):
    """Deterministic per-text vector; counts how often the API would be hit."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: List[str]) -> List[List[float]]:
        self.calls += 1
        return [[float(len(text))] + [0.0] * (_DIMS - 1) for text in texts]


@pytest.fixture
def embedder() -> _CountingEmbedder:
    return _CountingEmbedder()


@pytest.fixture
def make_cache(embedder: _CountingEmbedder,
               clean_db: str) -> Callable[..., QueryVectorCache]:
    """Factory for a cache against the empty canonical table."""
    def _make(model: str = 'text-embedding-3-small') -> QueryVectorCache:
        return QueryVectorCache(embedder, clean_db, model=model, dimensions=_DIMS)
    return _make


def test_miss_embeds_then_hit_reuses(make_cache, embedder):
    cache = make_cache()
    first = cache.get_vector('Bitcoin BTC')
    assert embedder.calls == 1          # miss → one embed
    second = cache.get_vector('Bitcoin BTC')
    assert embedder.calls == 1          # hit → no further embed
    assert first == second


def test_different_text_is_a_new_key(make_cache, embedder):
    cache = make_cache()
    cache.get_vector('Bitcoin BTC')
    cache.get_vector('Ethereum ETH')    # new text → new key → re-embed
    assert embedder.calls == 2


def test_model_change_is_a_new_key(make_cache, embedder):
    make_cache(model='model-a').get_vector('Bitcoin BTC')
    make_cache(model='model-b').get_vector('Bitcoin BTC')   # same text, new model → re-embed
    assert embedder.calls == 2
