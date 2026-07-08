"""Integration tests for QueryVectorCache — hit skips embed, text/model = new key.

Skipped when psycopg/pgvector or a reachable PostgreSQL is missing. The embedder is
faked (call-counting), so these spend no API budget — only the cache/DB logic is tested.
Point DATABASE_URL at a pgvector-enabled Postgres to run them.
"""
import os
from typing import List

import pytest

pytest.importorskip('psycopg')
pytest.importorskip('pgvector')
import psycopg  # noqa: E402

from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder  # noqa: E402
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache  # noqa: E402
from finiexragengine.exceptions.ragengine_errors import VectorStoreError  # noqa: E402

_DIMS = 4
_TABLE = 'query_vectors_test'


def _dsn() -> str:
    return os.environ.get(
        'DATABASE_URL', 'postgresql://ragengine:ragengine@127.0.0.1:5433/ragengine')


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
def make_cache(embedder):
    """Factory for a cache on a clean test table (dropped before and after)."""
    def _drop() -> None:
        with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {_TABLE}')

    try:
        _drop()
    except psycopg.Error as exc:
        pytest.skip(f'PostgreSQL not available: {exc}')

    def _make(model: str = 'text-embedding-3-small') -> QueryVectorCache:
        try:
            return QueryVectorCache(embedder, _dsn(), model=model,
                                    dimensions=_DIMS, table=_TABLE)
        except VectorStoreError as exc:
            pytest.skip(f'PostgreSQL/pgvector not available: {exc}')

    yield _make
    _drop()


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
