"""Query-vector cache (ISSUE_19) — embed the fixed retrieval queries once, keep them."""
from typing import List

import psycopg
from pgvector.psycopg import register_vector

from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.exceptions.ragengine_errors import VectorStoreError


def _to_float_list(value) -> List[float]:
    # pgvector deserialises the vector column to numpy when numpy is installed and to a
    # (non-iterable) pgvector.Vector otherwise. Mirror PgVectorStore's normaliser so the
    # cache works without numpy.
    if hasattr(value, 'to_list'):
        value = value.to_list()
    elif hasattr(value, 'tolist'):
        value = value.tolist()
    return [float(x) for x in value]


class QueryVectorCache:
    """Caches the embedded retrieval queries in Postgres (ISSUE_19).

    The retrieval queries are a fixed, small set (the constellation's `symbol_queries`),
    yet embedding the same text on every `retrieve()` call is a needless OpenAI request
    per symbol per eval cycle. This caches each query vector keyed by
    `(query_text, embedding_model, dimensions)`:

    - a cache hit returns the stored vector — no API call;
    - a changed query text is a different key and re-embeds only that query;
    - a changed model/dimensions is a different key too — vectors from different models
      live on different maps and must never mix (the "same map" invariant of ISSUE_16).

    Persisted next to the article corpus, so the fixed query vectors are also browsable
    and usable directly in SQL — e.g. joined against `articles.embedding` to reproduce the
    retriever's ranking by hand (see docs/development/database_inspection.md).
    """

    def __init__(self, embedder: AbstractEmbedder, database_url: str, model: str,
                 dimensions: int, table: str = 'query_vectors') -> None:
        self._embedder = embedder
        self._database_url = database_url
        self._model = model
        self._dimensions = dimensions
        self._table = table
        self._ensure_schema()

    def _raw_connect(self):
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the query-vector cache: {exc}') from exc

    def _connect(self):
        # register_vector needs the `vector` type to exist first (created in _ensure_schema).
        conn = self._raw_connect()
        register_vector(conn)
        return conn

    def _ensure_schema(self) -> None:
        try:
            with self._raw_connect() as conn, conn.cursor() as cur:
                cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
                cur.execute(
                    f'CREATE TABLE IF NOT EXISTS {self._table} ('
                    'query_text TEXT NOT NULL, '
                    'embedding_model TEXT NOT NULL, '
                    'dimensions INTEGER NOT NULL, '
                    f'embedding vector({self._dimensions}) NOT NULL, '
                    'created_at TIMESTAMPTZ NOT NULL DEFAULT now(), '
                    'PRIMARY KEY (query_text, embedding_model, dimensions))'
                )
        except psycopg.Error as exc:
            raise VectorStoreError(f'query-cache schema init failed: {exc}') from exc

    def get_vector(self, query_text: str) -> List[float]:
        """Return the query's embedding — from cache, or embed-and-store on a miss.

        Args:
            query_text: The retrieval query (e.g. from SymbolQueryMap.query_for).

        Returns:
            The embedding vector for `query_text` under the configured model.
        """
        key = (query_text, self._model, self._dimensions)
        select = (f'SELECT embedding FROM {self._table} '
                  'WHERE query_text = %s AND embedding_model = %s AND dimensions = %s')
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(select, key)
                row = cur.fetchone()
        except psycopg.Error as exc:
            raise VectorStoreError(f'query-cache lookup failed: {exc}') from exc
        if row is not None:
            return _to_float_list(row[0])          # cache hit — no API call

        # miss: embed once (outside any open transaction), then persist for later reuse
        vector = self._embedder.embed([query_text])[0]
        insert = (f'INSERT INTO {self._table} '
                  '(query_text, embedding_model, dimensions, embedding) '
                  'VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING')
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(insert, (query_text, self._model, self._dimensions, vector))
        except psycopg.Error as exc:
            raise VectorStoreError(f'query-cache write failed: {exc}') from exc
        return vector
