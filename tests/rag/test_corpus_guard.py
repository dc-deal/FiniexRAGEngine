"""Corpus embedding-model guard (ISSUE_16) — needs a reachable pgvector Postgres
(skipped otherwise), no API budget. The corpus is stamped with its embedding model in
the database itself; booting against a mismatched stamp must refuse hard.

Since ISSUE_14 the store creates no schema, so this exercises the guard alone: the stamp
round-trip and the two mismatches. Dimensions are the real ones (text-embedding-3-small = 1536,
-large = 3072) — the exact swap the guard exists to catch.
"""
import psycopg
import pytest

from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig

_SMALL, _LARGE = 1536, 3072


def _store(dsn: str, model: str = 'text-embedding-3-small',
           dims: int = _SMALL) -> PgVectorStore:
    return PgVectorStore(VectorStoreConfig(), dsn, dimensions=dims, embedding_model=model)


def test_fresh_corpus_is_stamped(clean_db):
    _store(clean_db)
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT embedding_model, dimensions FROM corpus_meta '
                    'WHERE table_name = %s', ('articles',))
        assert cur.fetchone() == ('text-embedding-3-small', _SMALL)


def test_matching_stamp_boots_normally(clean_db):
    _store(clean_db)
    _store(clean_db)                                   # same model + dims — boots as today


def test_model_mismatch_refuses_to_boot(clean_db):
    _store(clean_db, model='text-embedding-3-small')
    with pytest.raises(VectorStoreError, match='3-small.*3-large|3-large.*3-small'):
        _store(clean_db, model='text-embedding-3-large', dims=_LARGE)


def test_dimension_mismatch_refuses_to_boot(clean_db):
    # A model keeping its name but changing width would still pass a column check —
    # the stamp compares dimensions explicitly, so it catches that too.
    _store(clean_db, dims=_SMALL)
    with pytest.raises(VectorStoreError, match='1536 dims.*3072 dims|3072 dims.*1536 dims'):
        _store(clean_db, dims=_LARGE)
