"""Tests for cost derivation + recording (ISSUE_23).

`test_derive_*` is pure math. `test_record_*` writes to a cost_log table and needs a
reachable pgvector Postgres (skipped otherwise) — no API budget is touched.
"""
import os

import pytest

pytest.importorskip('psycopg')
import psycopg  # noqa: E402

from finiexragengine.core.observability.cost_recorder import (  # noqa: E402
    CostRecorder,
    derive_usd,
)
from finiexragengine.exceptions.ragengine_errors import VectorStoreError  # noqa: E402
from finiexragengine.types.config_types.app_config_types import (  # noqa: E402
    ModelPrice,
    PricingConfig,
)

_TABLE = 'cost_log_test'
_PRICING = PricingConfig(models={
    'text-embedding-3-small': ModelPrice(input_per_1k=0.00002),
    'gpt-4o-mini': ModelPrice(input_per_1k=0.00015, output_per_1k=0.0006),
})


def _dsn() -> str:
    return os.environ.get(
        'DATABASE_URL', 'postgresql://ragengine:ragengine@127.0.0.1:5433/ragengine')


def test_derive_usd_embedding_input_only():
    assert derive_usd(_PRICING, 'text-embedding-3-small', 10_000) == pytest.approx(0.0002)


def test_derive_usd_chat_input_plus_output():
    # 1000/1k*0.00015 + 500/1k*0.0006 = 0.00015 + 0.0003
    assert derive_usd(_PRICING, 'gpt-4o-mini', 1000, 500) == pytest.approx(0.00045)


def test_derive_usd_unknown_model_is_zero():
    assert derive_usd(_PRICING, 'mystery-model', 1000, 1000) == 0.0


@pytest.fixture
def recorder():
    def _drop() -> None:
        with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {_TABLE}')
    try:
        _drop()
        rec = CostRecorder(_dsn(), _PRICING, table=_TABLE)
    except (psycopg.Error, VectorStoreError) as exc:
        pytest.skip(f'PostgreSQL not available: {exc}')
    yield rec
    _drop()


def test_record_writes_row_and_returns_usd(recorder):
    usd = recorder.record('ingest_news', 'text-embedding-3-small', 10_000, pipeline_id='p')
    assert usd == pytest.approx(0.0002)
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT section, model, total_tokens, usd_cost, pipeline_id FROM {_TABLE}')
        row = cur.fetchone()
    assert row[0] == 'ingest_news'
    assert row[1] == 'text-embedding-3-small'
    assert row[2] == 10_000
    assert row[3] == pytest.approx(0.0002)
    assert row[4] == 'p'
