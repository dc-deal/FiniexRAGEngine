"""Weekly report (ISSUE_27) — model composition, staleness derivation, console render.

Render tests build the typed model in memory (no DB); the status-collection test runs
against the migration-built test schema (skipped without Postgres), no API budget.
"""
from datetime import datetime, timedelta, timezone

import pytest

from finiexragengine.core.observability.reports.breaking_report import BreakingReport
from finiexragengine.core.observability.reports.cost_report import CostReport
from finiexragengine.core.observability.reports.no_data_report import NoDataReport
from finiexragengine.core.observability.reports.perf_report import PerfReport
from finiexragengine.core.observability.reports.source_health_report import SourceHealthReport
from finiexragengine.core.observability.reports.weekly_report import (
    ErrorTypeCount,
    PipelineStatusRow,
    StorageStats,
    WeeklyReport,
    _cadence_for,
    _collect_status,
    format_weekly_report,
)
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.types.outcome_types import (
    RunError,
    RunMetadata,
    SentimentEnvelope,
    SentimentResult,
)

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _model(pipelines=(), errors=(), last_ingest=None) -> WeeklyReport:
    """A minimal in-memory weekly model — empty sub-reports, focus on the weekly sections."""
    return WeeklyReport(
        since=_NOW - timedelta(days=7), until=_NOW,
        cost=CostReport(real=[], prediction=None, spent_all_usd=0.0),
        perf=PerfReport('7d', [], 0),
        sources=SourceHealthReport([], []),
        no_data=NoDataReport('7d', [], 0),
        breaking=BreakingReport('7d', [], 0, 0),
        pipelines=list(pipelines), errors=list(errors),
        storage=StorageStats(1312, 87, 1841, 214 * 1024 ** 2),
        last_ingest_at=last_ingest)


def test_format_stitches_all_sections_under_one_header():
    text = format_weekly_report(_model())
    assert 'FiniexRAGEngine — Weekly Report' in text
    assert 'window: 2026-07-13 → 2026-07-20 (UTC)' in text
    # Every section surfaces exactly once — reused formatters + the weekly-specific two.
    for title in ('Cost', 'Performance', 'Source', 'Retrieval Coverage',
                  'Breaking Detection', 'Storage', 'Status'):
        assert title in text, title


def test_format_status_and_storage_lines():
    rows = [
        PipelineStatusRow('crypto_sentiment', 600, 201, 190, 11, 0,
                          _NOW - timedelta(minutes=4), stale=False),
        PipelineStatusRow('forex_macro_sentiment', 600, 12, 12, 0, 0,
                          _NOW - timedelta(hours=26), stale=True),
    ]
    text = format_weekly_report(_model(
        pipelines=rows, errors=[ErrorTypeCount('SOURCE_UNREACHABLE', 7)],
        last_ingest=_NOW - timedelta(minutes=9)))
    assert '190/11/0' in text
    assert '⚠ STALE' in text and text.count('⚠ STALE') == 1
    assert 'corpus 1312 articles (+87 this week) · envelopes 1841 · DB 214 MB' in text
    assert 'errors this week: 7 SOURCE_UNREACHABLE' in text
    assert 'ingest: last poll 9m ago' in text


def test_cadence_lookup_tolerates_variant_stream_ids():
    cadences = {'crypto_sentiment': 600}
    assert _cadence_for('crypto_sentiment', cadences) == 600
    assert _cadence_for('crypto_sentiment_4o_enhanced', cadences) == 600   # fan-out stream
    assert _cadence_for('unknown', cadences) is None


def _envelope(pipeline_id, ts, status='success') -> SentimentEnvelope:
    result = [] if status == 'error' else [SentimentResult(
        symbol='BTCUSD', signal='HOLD', sentiment_score=0.0, confidence=0.5, reasoning='r')]
    errors = ([RunError(type='LLM_TIMEOUT', message='slow', timestamp=ts)]
              if status == 'error' else [])
    return SentimentEnvelope(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed', prompt_version='2',
        prompt_id='sentiment-crypto', prompt_hash='1c86eac137d8', timestamp=ts,
        status=status, result=result,
        metadata=RunMetadata(model='gpt-4o-mini', model_snapshot='gpt-4o-mini-2024-07-18'),
        errors=errors)


def test_collect_status_census_staleness_and_errors(clean_db):
    now = datetime.now(timezone.utc)
    store = OutcomeStore(clean_db)
    store.save(_envelope('fresh_pipe', now - timedelta(minutes=5)))
    store.save(_envelope('fresh_pipe', now - timedelta(minutes=15), status='error'))
    store.save(_envelope('dead_pipe', now - timedelta(hours=26)))

    pipelines, errors, storage, _last_ingest = _collect_status(
        clean_db, now - timedelta(days=7), now,
        {'fresh_pipe': 600, 'dead_pipe': 600})

    by_id = {row.pipeline_id: row for row in pipelines}
    assert by_id['fresh_pipe'].passes == 2
    assert (by_id['fresh_pipe'].success, by_id['fresh_pipe'].error) == (1, 1)
    assert not by_id['fresh_pipe'].stale
    assert by_id['dead_pipe'].stale                     # 26h silent at a 10m cadence
    assert ErrorTypeCount('LLM_TIMEOUT', 1) in errors   # taxonomy from the envelopes
    assert storage.outcomes_total == 3
    assert storage.db_bytes > 0
