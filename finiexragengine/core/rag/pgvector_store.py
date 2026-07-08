"""pgvector-backed vector store (PostgreSQL)."""
from datetime import datetime
from typing import List, Optional

import psycopg
from pgvector.psycopg import register_vector

from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.article_types import Article, ScoredArticle
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig


def _to_float_list(value) -> List[float]:
    # pgvector deserialises the `vector` column to a numpy array when numpy is
    # installed and to a (non-iterable) pgvector.Vector otherwise. Normalise both
    # — and a plain list — to a list of floats so retrieval works without numpy.
    if hasattr(value, 'to_list'):
        value = value.to_list()
    elif hasattr(value, 'tolist'):
        value = value.tolist()
    return [float(x) for x in value]


class PgVectorStore(AbstractVectorStore):
    """Stores article embeddings in a pgvector column — the shared corpus.

    Idempotent on article_id (ISSUE_3); retains raw article fields + timestamps
    so the corpus can be replayed (ISSUE_4). The `importance` / `breaking_candidate`
    columns are created now (nullable) and populated later by the breaking detector
    (ISSUE_11). The AbstractVectorStore contract lets Chroma swap in later.
    """

    _COLUMNS = ('article_id', 'source_id', 'source_weight', 'url', 'title',
                'summary', 'language', 'published_at', 'fetched_at')

    def __init__(self, config: VectorStoreConfig, database_url: str, dimensions: int) -> None:
        self._config = config
        self._database_url = database_url
        self._dimensions = dimensions
        self._ensure_schema()

    def _raw_connect(self):
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the vector store: {exc}') from exc

    def _connect(self):
        # register_vector needs the `vector` type to already exist, so this is only
        # used after _ensure_schema has created the extension.
        conn = self._raw_connect()
        register_vector(conn)
        return conn

    def _ensure_schema(self) -> None:
        table = self._config.table
        try:
            with self._raw_connect() as conn, conn.cursor() as cur:
                cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
                cur.execute(
                    f'CREATE TABLE IF NOT EXISTS {table} ('
                    'article_id TEXT PRIMARY KEY, '
                    'source_id TEXT NOT NULL, '
                    'source_weight REAL NOT NULL, '
                    'url TEXT NOT NULL, '
                    'title TEXT NOT NULL, '
                    'summary TEXT NOT NULL, '
                    'language TEXT NOT NULL, '
                    'published_at TIMESTAMPTZ NOT NULL, '
                    'fetched_at TIMESTAMPTZ NOT NULL, '
                    f'embedding vector({self._dimensions}) NOT NULL, '
                    'importance SMALLINT, '
                    'breaking_candidate BOOLEAN NOT NULL DEFAULT FALSE)'
                )
                # No ANN index yet: cosine search is an exact full scan, which is
                # fine at the current corpus size. Add an HNSW index on `embedding`
                # (vector_cosine_ops) before the scan dominates query latency.
        except psycopg.Error as exc:
            raise VectorStoreError(f'schema init failed: {exc}') from exc

    def upsert(self, articles: List[Article], vectors: List[List[float]]) -> int:
        if len(articles) != len(vectors):
            raise VectorStoreError('articles and vectors must be the same length')
        if not articles:
            return 0
        table = self._config.table
        sql = (
            f'INSERT INTO {table} (article_id, source_id, source_weight, url, title, '
            'summary, language, published_at, fetched_at, embedding) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
            'ON CONFLICT (article_id) DO NOTHING'
        )
        written = 0
        try:
            with self._connect() as conn, conn.cursor() as cur:
                for article, vector in zip(articles, vectors):
                    cur.execute(sql, (
                        article.article_id, article.source_id, article.source_weight,
                        article.url, article.title, article.summary, article.language,
                        article.published_at, article.fetched_at, vector,
                    ))
                    written += cur.rowcount
        except psycopg.Error as exc:
            raise VectorStoreError(f'upsert failed: {exc}') from exc
        return written

    def query(self, vector: List[float], top_k: int, since: datetime,
              min_importance: Optional[int] = None) -> List[ScoredArticle]:
        table = self._config.table
        columns = ', '.join(self._COLUMNS)
        # <=> is pgvector's cosine-distance operator (0.0 = identical direction).
        # Recency filter, distance ranking and the fetch cap run in one round-trip.
        # The query set is fixed per constellation, so query vectors and these
        # distances are cacheable (materialize at ingest) — today each call embeds
        # and scans afresh; revisit when the corpus outgrows the exact scan.
        sql = (f'SELECT {columns}, importance, embedding, '
               f'embedding <=> %s::vector AS distance '
               f'FROM {table} WHERE published_at >= %s')
        params: list = [vector, since]
        if min_importance is not None:
            sql += ' AND importance >= %s'
            params.append(min_importance)
        sql += ' ORDER BY distance LIMIT %s'
        params.append(top_k)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f'query failed: {exc}') from exc
        n = len(self._COLUMNS)
        return [ScoredArticle(
            article=Article(*row[:n]),
            distance=float(row[n + 2]),
            embedding=_to_float_list(row[n + 1]),
            importance=row[n],
        ) for row in rows]
