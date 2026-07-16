"""Tests for the performance report (ISSUE_32).

`test_format_*` is pure rendering (no DB). `test_build_*` seeds the canonical `cost_log` in the
isolated, migration-built test schema (`clean_db`, ISSUE_14) and needs a reachable Postgres —
skipped otherwise; no API budget.
"""
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip('psycopg')
import psycopg  # noqa: E402

from finiexragengine.core.observability.cost_recorder import CostRecorder  # noqa: E402
from finiexragengine.core.observability.reports.perf_report import (  # noqa: E402
    PerfReport,
    SectionPerf,
    build_perf_report,
    format_perf_report,
)
from finiexragengine.types.config_types.app_config_types import (  # noqa: E402
    ModelPrice,
    PricingConfig,
)

_TABLE = 'cost_log'
_PRICING = PricingConfig(models={
    'text-embedding-3-small': ModelPrice(input_per_1k=0.00002),
})


def test_format_shows_pattern_table_and_untimed_note():
    report = PerfReport(
        since_label='7d',
        rows=[SectionPerf('llm_eval', 7, 2650.0, 3100.0, 3210.0, 18.6),
              SectionPerf('ingest_news', 10, 820.0, 1400.0, 1520.0, 8.2)],
        untimed_calls=3)
    text = format_perf_report(report)
    assert 'Performance Report' in text
    assert '---' in text                                   # the ---- pattern dividers
    assert 'llm_eval' in text and 'ingest_news' in text
    assert 'p95 ms' in text
    assert 'untimed legacy calls excluded: 3' in text


def test_format_empty_window():
    report = PerfReport(since_label='all-time', rows=[], untimed_calls=0)
    text = format_perf_report(report)
    assert '(no timed API calls in the window)' in text
    assert 'untimed' not in text                           # note only when relevant


@pytest.fixture
def seeded(clean_db: str) -> str:
    rec = CostRecorder(clean_db, _PRICING)
    rec.record('llm_eval', 'text-embedding-3-small', 1000, duration_ms=2000.0)
    rec.record('llm_eval', 'text-embedding-3-small', 1000, duration_ms=3000.0)
    rec.record('ingest_news', 'text-embedding-3-small', 500, duration_ms=800.0)
    rec.record('ingest_news', 'text-embedding-3-small', 500)   # legacy: no duration
    return clean_db


def test_build_aggregates_latency_by_section(seeded):
    since = datetime.now(timezone.utc) - timedelta(days=1)
    report = build_perf_report(seeded, since)
    by_section = {r.section: r for r in report.rows}
    assert set(by_section) == {'llm_eval', 'ingest_news'}
    llm = by_section['llm_eval']
    assert llm.calls == 2
    assert llm.avg_ms == pytest.approx(2500.0)
    assert llm.max_ms == pytest.approx(3000.0)
    assert llm.api_seconds == pytest.approx(5.0)
    assert report.untimed_calls == 1                       # the row without a duration
    assert report.window_api_seconds == pytest.approx(5.8)


def test_build_survives_missing_table(db_dsn):
    # The read-only report must answer 'nothing to report', never crash, when the table it
    # points at does not exist.
    since = datetime.now(timezone.utc) - timedelta(days=1)
    report = build_perf_report(db_dsn, since, table='cost_log_never_created')
    assert report.rows == [] and report.untimed_calls == 0


def test_build_survives_legacy_table_without_duration_column(db_dsn):
    # A cost_log written before ISSUE_32 (no duration_ms) — every row is untimed legacy.
    table = 'cost_log_perf_legacy_test'
    try:
        with psycopg.connect(db_dsn) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {table}')
            cur.execute(
                f'CREATE TABLE {table} ('
                'id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), '
                'section TEXT NOT NULL, model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, '
                'completion_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT NULL, '
                'usd_cost DOUBLE PRECISION NOT NULL, pipeline_id TEXT)')
            cur.execute(f"INSERT INTO {table} (section, model, prompt_tokens, total_tokens, "
                        f"usd_cost) VALUES ('llm_eval', 'gpt-4o-mini', 100, 150, 0.0001)")
    except psycopg.Error as exc:
        pytest.skip(f'PostgreSQL not available: {exc}')
    try:
        since = datetime.now(timezone.utc) - timedelta(days=1)
        report = build_perf_report(db_dsn, since, table=table)
        assert report.rows == []
        assert report.untimed_calls == 1                   # legacy row counted, not crashed
    finally:
        with psycopg.connect(db_dsn) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {table}')
