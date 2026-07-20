"""Telegram weekly rendering (ISSUE_27) — model → HTML sections, packing, escaping."""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.alerts.telegram_weekly_format import (
    _pack,
    render_weekly_messages,
)
from finiexragengine.core.observability.reports.breaking_report import BreakingReport
from finiexragengine.core.observability.reports.cost_report import CostReport, RealWindow
from finiexragengine.core.observability.reports.no_data_report import NoDataReport, NoDataRow
from finiexragengine.core.observability.reports.perf_report import PerfReport
from finiexragengine.core.observability.reports.source_health_report import SourceHealthReport
from finiexragengine.core.observability.reports.weekly_report import (
    ErrorTypeCount,
    PipelineStatusRow,
    StorageStats,
    WeeklyReport,
)

_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _model(**overrides) -> WeeklyReport:
    fields = dict(
        since=_NOW - timedelta(days=7), until=_NOW,
        cost=CostReport(real=[RealWindow('this week', 214, 412_000, 0.081)],
                        prediction=None, spent_all_usd=0.264, credit_usd=5.0),
        perf=PerfReport('7d', [], 0),
        sources=SourceHealthReport([], []),
        no_data=NoDataReport('7d', [], 16),
        breaking=BreakingReport('7d', [], 3, 1),
        pipelines=[PipelineStatusRow('crypto_sentiment', 600, 201, 190, 11, 0,
                                     _NOW - timedelta(minutes=4), stale=False)],
        errors=[ErrorTypeCount('SOURCE_UNREACHABLE', 7)],
        storage=StorageStats(1312, 87, 1841, 214 * 1024 ** 2),
        last_ingest_at=_NOW - timedelta(minutes=9))
    fields.update(overrides)
    return WeeklyReport(**fields)


def test_clean_week_renders_one_message_with_quiet_lines():
    messages = render_weekly_messages(_model())
    assert len(messages) == 1
    text = messages[0]
    assert '<b>FiniexRAGEngine — Weekly Report</b>' in text
    assert 'this week: 214 calls · 412k tok · $0.0810' in text
    assert 'credit $5.00 → $4.7360 remaining' in text
    assert 'all feeds healthy' in text
    assert 'all 16 symbols delivering' in text
    assert 'crypto_sentiment: last pass 4m ago · 190/11/0 ok/part/err' in text
    assert 'errors: 7 SOURCE_UNREACHABLE' in text


def test_candidate_symbol_and_stale_worker_are_flagged():
    no_data = NoDataReport('7d', [NoDataRow(
        'crypto_sentiment', 'ETHUSD', passes=31, no_data_passes=21,
        nearest_miss_min=0.681, nearest_miss_avg=0.71, floor=0.70, kept_avg=2.0,
        candidate=True)], 16)
    stale = PipelineStatusRow('forex_macro_sentiment', 600, 12, 12, 0, 0,
                              _NOW - timedelta(hours=26), stale=True)
    text = render_weekly_messages(_model(no_data=no_data, pipelines=[stale]))[0]
    assert 'ETHUSD 68% no-data · miss 0.681 vs floor 0.70 ⚠ candidate' in text
    assert 'forex_macro_sentiment: last pass 26h ago · 12/0/0 ok/part/err ⚠ STALE' in text


def test_dynamic_text_is_html_escaped():
    errors = [ErrorTypeCount('<script>', 1)]
    text = render_weekly_messages(_model(errors=errors))[0]
    assert '<script>' not in text
    assert '&lt;script&gt;' in text


def test_packing_splits_only_at_section_bounds_and_respects_the_limit():
    sections = ['a' * 2000, 'b' * 2000, 'c' * 100]
    packed = _pack(sections)
    assert packed == ['a' * 2000, 'b' * 2000 + '\n\n' + 'c' * 100]
    assert all(len(message) <= 4096 for message in packed)
