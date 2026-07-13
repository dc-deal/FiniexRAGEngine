"""Corpus embedding-model guard (ISSUE_16) — needs a reachable pgvector Postgres
(skipped otherwise), no API budget. The corpus is stamped with its embedding model in
the database itself; booting against a mismatched stamp must refuse hard.
"""
import os

import pytest

pytest.importorskip('psycopg')
import psycopg  # noqa: E402

from finiexragengine.core.rag.pgvector_store import PgVectorStore  # noqa: E402
from finiexragengine.exceptions.ragengine_errors import VectorStoreError  # noqa: E402
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig  # noqa: E402

_TABLE = 'articles_guard_test'
_DIMS = 4


def _dsn() -> str:
    return os.environ.get(
        'DATABASE_URL', 'postgresql://ragengine:ragengine@127.0.0.1:5433/ragengine')


def _store(model: str = 'model-a', dims: int = _DIMS) -> PgVectorStore:
    return PgVectorStore(VectorStoreConfig(table=_TABLE), _dsn(),
                         dimensions=dims, embedding_model=model)


@pytest.fixture(autouse=True)
def clean_corpus():
    def _drop() -> None:
        with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {_TABLE}')
            cur.execute("SELECT to_regclass('corpus_meta')")
            if cur.fetchone()[0] is not None:
                cur.execute('DELETE FROM corpus_meta WHERE table_name = %s', (_TABLE,))
    try:
        _drop()
    except psycopg.Error as exc:
        pytest.skip(f'PostgreSQL/pgvector not available: {exc}')
    yield
    _drop()


def test_fresh_corpus_is_stamped():
    _store()
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute('SELECT embedding_model, dimensions FROM corpus_meta '
                    'WHERE table_name = %s', (_TABLE,))
        assert cur.fetchone() == ('model-a', _DIMS)


def test_matching_stamp_boots_normally():
    _store()
    _store()                                       # same model + dims — boots as today


def test_model_mismatch_refuses_to_boot():
    _store(model='model-a')
    with pytest.raises(VectorStoreError, match='model-a.*model-b|model-b.*model-a'):
        _store(model='model-b')


def test_dimension_mismatch_refuses_to_boot():
    # Same 1536-style trap: another model with identical dims would pass the column
    # width — the stamp catches the model; a dims change is caught explicitly too.
    _store(dims=4)
    with pytest.raises(VectorStoreError, match='4 dims.*8 dims|8 dims.*4 dims'):
        _store(dims=8)
