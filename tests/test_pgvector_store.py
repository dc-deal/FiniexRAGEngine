"""Integration tests for PgVectorStore — idempotency, recency, ordering, importance.

Skipped when psycopg/pgvector or a reachable PostgreSQL is missing, so the suite stays
green everywhere. Point DATABASE_URL at a pgvector-enabled Postgres to run them.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip('psycopg')
pytest.importorskip('pgvector')
import psycopg  # noqa: E402

from finiexragengine.core.rag.pgvector_store import PgVectorStore  # noqa: E402
from finiexragengine.exceptions.ragengine_errors import VectorStoreError  # noqa: E402
from finiexragengine.types.article_types import Article  # noqa: E402
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig  # noqa: E402

_DIMS = 4
_TABLE = 'articles_test'
_BASE = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)


def _dsn() -> str:
    return os.environ.get(
        'DATABASE_URL', 'postgresql://ragengine:ragengine@127.0.0.1:5433/ragengine')


def _article(article_id: str, published_at: datetime) -> Article:
    return Article(
        article_id=article_id, source_id='s', source_weight=1.0,
        url=f'https://example.test/{article_id}', title=f'title-{article_id}',
        summary='summary', language='en', published_at=published_at, fetched_at=_BASE)


@pytest.fixture
def store():
    config = VectorStoreConfig(table=_TABLE)
    try:
        instance = PgVectorStore(config, _dsn(), dimensions=_DIMS,
                                 embedding_model='test-embed')
    except VectorStoreError as exc:
        pytest.skip(f'PostgreSQL/pgvector not available: {exc}')
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(f'TRUNCATE {_TABLE}')
    yield instance
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS {_TABLE}')
        cur.execute('DELETE FROM corpus_meta WHERE table_name = %s', (_TABLE,))


def test_upsert_is_idempotent(store):
    arts = [_article('a', _BASE), _article('b', _BASE)]
    vecs = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    assert store.upsert(arts, vecs) == 2
    assert store.upsert(arts, vecs) == 0  # conflicts skipped → idempotent


def test_query_recency_and_similarity_order(store):
    old = _BASE - timedelta(days=10)
    store.upsert(
        [_article('near', _BASE), _article('far', _BASE), _article('old', old)],
        [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]],
    )
    result = store.query([1.0, 0.0, 0.0, 0.0], top_k=10, since=_BASE - timedelta(days=1))
    ids = [hit.article.article_id for hit in result]
    assert 'old' not in ids          # recency lower bound excludes the stale article
    assert ids[0] == 'near'          # identical vector → most similar first
    assert result[0].distance <= result[1].distance      # cosine distance, ascending
    assert result[0].embedding == [1.0, 0.0, 0.0, 0.0]   # stored embedding round-trips
    assert result[0].importance is None                  # tag populated later by #11


def test_query_min_importance_excludes_null(store):
    # upsert leaves importance NULL (populated later by #11) → filtered out when required
    store.upsert([_article('a', _BASE)], [[1.0, 0.0, 0.0, 0.0]])
    result = store.query([1.0, 0.0, 0.0, 0.0], top_k=10,
                         since=_BASE - timedelta(days=1), min_importance=2)
    assert result == []
