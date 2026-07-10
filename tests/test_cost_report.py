"""Tests for the cost report (ISSUE_23).

`test_format_*` is pure rendering (no DB). `test_build_*` seeds a cost_log and needs a
reachable pgvector Postgres — skipped otherwise; no API budget is touched.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip('psycopg')
import psycopg  # noqa: E402

from finiexragengine.core.observability.cost_recorder import CostRecorder  # noqa: E402
from finiexragengine.core.observability.cost_report import (  # noqa: E402
    CostReport,
    SectionCost,
    build_cost_report,
    format_cost_report,
)
from finiexragengine.exceptions.ragengine_errors import VectorStoreError  # noqa: E402
from finiexragengine.types.config_types.app_config_types import (  # noqa: E402
    ModelPrice,
    PricingConfig,
)

_TABLE = 'cost_log_report_test'
_PRICING = PricingConfig(models={
    'text-embedding-3-small': ModelPrice(input_per_1k=0.00002),
})


def _dsn() -> str:
    return os.environ.get(
        'DATABASE_URL', 'postgresql://ragengine:ragengine@127.0.0.1:5433/ragengine')


def test_format_shows_sections_and_derived_balance():
    report = CostReport(
        since_label='7d',
        rows=[SectionCost('ingest_news', 3, 12000, 0.24),
              SectionCost('llm_eval', 1, 800, 0.05)],
        window_usd=0.29, window_tokens=12800, spent_all_usd=0.29,
        credit_usd=50.0, budget_usd=0.0)
    text = format_cost_report(report)
    assert 'ingest_news' in text and 'llm_eval' in text
    assert 'window total' in text
    assert 'remaining' in text and '49.71' in text     # 50.00 − 0.29


def test_format_without_credit_hints_and_empty_window():
    report = CostReport(since_label='all-time', rows=[], window_usd=0.0, window_tokens=0,
                        spent_all_usd=0.0, credit_usd=0.0, budget_usd=0.0)
    text = format_cost_report(report)
    assert 'not set' in text
    assert '(no paid calls in the window)' in text


@pytest.fixture
def seeded():
    def _drop() -> None:
        with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {_TABLE}')
    try:
        _drop()
        rec = CostRecorder(_dsn(), _PRICING, table=_TABLE)
    except (psycopg.Error, VectorStoreError) as exc:
        pytest.skip(f'PostgreSQL not available: {exc}')
    rec.record('ingest_news', 'text-embedding-3-small', 10_000)   # 0.0002
    rec.record('ingest_query', 'text-embedding-3-small', 1_000)   # 0.00002
    yield
    _drop()


def test_build_aggregates_by_section(seeded):
    since = datetime.now(timezone.utc) - timedelta(days=1)
    report = build_cost_report(_dsn(), since, credit_usd=10.0, table=_TABLE)
    by_section = {r.section: r for r in report.rows}
    assert set(by_section) == {'ingest_news', 'ingest_query'}
    assert by_section['ingest_news'].usd == pytest.approx(0.0002)
    assert report.spent_all_usd == pytest.approx(0.00022)
    assert report.remaining_usd == pytest.approx(10.0 - 0.00022)


def test_build_survives_missing_table():
    # Fresh DB, no CostRecorder ever ran: 'nothing spent yet', not a crash.
    since = datetime.now(timezone.utc) - timedelta(days=1)
    try:
        report = build_cost_report(_dsn(), since, credit_usd=5.0,
                                   table='cost_log_never_created')
    except VectorStoreError as exc:
        pytest.skip(f'PostgreSQL not available: {exc}')
    assert report.rows == [] and report.spent_all_usd == 0.0
    assert report.remaining_usd == pytest.approx(5.0)      # credit passes through
