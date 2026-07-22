"""Weekly report — one typed model for every delivery surface (ISSUE_27).

The "how was the week" summary: cost, performance, source health, retrieval coverage,
breaking funnel, storage and worker status — **one `WeeklyReport` object**, composed from
the existing report builders (report-from-the-store, no re-measuring) plus the
weekly-specific status/storage aggregation below. Renderers generate the printouts from
the model: `format_weekly_report` (console, launch.json / report_cli) and the Telegram
rendering in `core/alerts/` — same code path, two worlds.

Worker-death is a *derived* signal (there is no heartbeat table): an eval pipeline whose
newest envelope is older than `_STALE_FACTOR ×` its configured cadence is marked stale;
ingest liveness comes from the newest `source_health` poll timestamp.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import psycopg

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.breaking_report import (
    BreakingReport,
    build_breaking_report,
    format_breaking_report,
)
from finiexragengine.core.observability.reports.cost_report import (
    CostReport,
    EvalPipelineInfo,
    build_cost_report,
    format_cost_report,
)
from finiexragengine.core.observability.reports.no_data_report import (
    NoDataReport,
    build_no_data_report,
    format_no_data_report,
)
from finiexragengine.core.observability.reports.perf_report import (
    PerfReport,
    build_perf_report,
    format_perf_report,
)
from finiexragengine.core.observability.reports.source_health_report import (
    SourceHealthReport,
    build_source_health_report,
    format_source_health_report,
)
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

_WINDOW = timedelta(days=7)
# An eval stream is presumed dead when its newest envelope is older than this many cadences.
_STALE_FACTOR = 3


@dataclass
class PipelineStatusRow:
    """One eval stream's weekly pass census + the derived liveness verdict."""
    pipeline_id: str
    cadence_seconds: Optional[int]     # effective config cadence (None = unknown stream)
    passes: int
    success: int
    partial: int
    error: int
    last_ts: Optional[datetime]
    stale: bool                        # last_ts older than _STALE_FACTOR × cadence


@dataclass
class ErrorTypeCount:
    """One RunError taxonomy type's weekly occurrence count (from persisted envelopes)."""
    type: str
    count: int


@dataclass
class StorageStats:
    articles_total: int
    articles_week: int                 # fetched into the corpus inside the window
    outcomes_total: int
    db_bytes: int


@dataclass
class WeeklyReport:
    """The typed weekly model — every renderer (console, Telegram) reads only this."""
    since: datetime
    until: datetime
    cost: CostReport
    perf: PerfReport
    sources: SourceHealthReport
    no_data: NoDataReport
    breaking: BreakingReport
    pipelines: List[PipelineStatusRow]
    errors: List[ErrorTypeCount]
    storage: StorageStats
    last_ingest_at: Optional[datetime]


def collect_weekly_report(config_manager: AppConfigManager, database_url: str, *,
                          now: Optional[datetime] = None) -> WeeklyReport:
    """The one composition entry point — CLI, scheduler and /report all call this.

    Wires the effective config (registries via the manager factories — user overrides
    included) into the existing builders, then adds the weekly-specific status/storage
    aggregation. Pure DB reads; no paid calls.
    """
    until = now or datetime.now(timezone.utc)
    since = until - _WINDOW
    cfg = config_manager.get_config()

    # Effective eval cadences — same wiring as cost_cli (projection + staleness need it).
    registry = config_manager.build_pipeline_registry()
    eval_pipelines = {
        p.get_config().pipeline_id: EvalPipelineInfo(
            interval_seconds=p.get_config().trigger.cadence_seconds,
            symbol_count=len(p.get_config().symbols),
            overridden=registry.is_overridden(p.get_config().pipeline_id))
        for p in registry.list_pipelines()}
    # Configured/disabled source ids — same wiring as sources_cli (orphan + disabled marks).
    source_sets = config_manager.build_source_set_registry()
    configured_ids = {source.source_id for source_set in source_sets.list_sets()
                      for source in source_set.sources}
    disabled_ids = {source.source_id for source_set in source_sets.list_sets()
                    for source in source_set.sources if not source.enabled}

    cadences = {pid: info.interval_seconds for pid, info in eval_pipelines.items()}
    pipelines, errors, storage, last_ingest = _collect_status(
        database_url, since, until, cadences)

    return WeeklyReport(
        since=since, until=until,
        cost=build_cost_report(database_url, eval_pipelines=eval_pipelines,
                               credit_usd=cfg.cost.account_credit_usd),
        perf=build_perf_report(database_url, since),
        sources=build_source_health_report(database_url, configured_ids,
                                           disabled_ids=disabled_ids),
        no_data=build_no_data_report(database_url, since),
        breaking=build_breaking_report(database_url, since),
        pipelines=pipelines, errors=errors, storage=storage, last_ingest_at=last_ingest)


def _table_exists(cur: psycopg.Cursor, table: str) -> bool:
    cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s', (table,))
    return cur.fetchone()[0] > 0


def _collect_status(database_url: str, since: datetime, until: datetime,
                    cadences: Dict[str, Optional[int]],
                    ) -> Tuple[List[PipelineStatusRow], List[ErrorTypeCount],
                               StorageStats, Optional[datetime]]:
    """The weekly-specific store reads: pass census, error taxonomy, storage, ingest pulse."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            pipelines: List[PipelineStatusRow] = []
            errors: List[ErrorTypeCount] = []
            outcomes_total = 0
            if _table_exists(cur, 'outcomes'):
                # Pass census per stream. Newest ts deliberately WITHOUT the window filter —
                # a dead worker's last envelope may be older than the week, and that is
                # exactly the signal.
                cur.execute(
                    'SELECT o.pipeline_id, count(*) FILTER (WHERE o.ts >= %s), '
                    "count(*) FILTER (WHERE o.ts >= %s AND o.status = 'success'), "
                    "count(*) FILTER (WHERE o.ts >= %s AND o.status = 'partial'), "
                    "count(*) FILTER (WHERE o.ts >= %s AND o.status = 'error'), "
                    'max(o.ts) FROM outcomes o GROUP BY o.pipeline_id ORDER BY o.pipeline_id',
                    (since, since, since, since))
                for pid, passes, ok, part, err, last_ts in cur.fetchall():
                    cadence = _cadence_for(pid, cadences)
                    stale = (cadence is not None and last_ts is not None
                             and (until - last_ts).total_seconds() > _STALE_FACTOR * cadence)
                    pipelines.append(PipelineStatusRow(
                        pid, cadence, int(passes), int(ok), int(part), int(err),
                        last_ts, stale))
                # RunError taxonomy over the window's envelopes — errors are structured,
                # never parsed from log text.
                cur.execute(
                    "SELECT err->>'type', count(*) FROM outcomes, "
                    "jsonb_array_elements(envelope->'errors') err "
                    'WHERE ts >= %s GROUP BY 1 ORDER BY 2 DESC', (since,))
                errors = [ErrorTypeCount(t or '(untyped)', int(c)) for t, c in cur.fetchall()]
                cur.execute('SELECT count(*) FROM outcomes')
                outcomes_total = int(cur.fetchone()[0])

            articles_total = articles_week = 0
            if _table_exists(cur, 'articles'):
                cur.execute('SELECT count(*), count(*) FILTER (WHERE fetched_at >= %s) '
                            'FROM articles', (since,))
                articles_total, articles_week = (int(v) for v in cur.fetchone())

            last_ingest: Optional[datetime] = None
            if _table_exists(cur, 'source_health'):
                cur.execute('SELECT max(updated_at) FROM source_health')
                last_ingest = cur.fetchone()[0]

            cur.execute('SELECT pg_database_size(current_database())')
            db_bytes = int(cur.fetchone()[0])
    except psycopg.Error as exc:
        raise VectorStoreError(f'weekly status collection failed: {exc}') from exc

    return (pipelines, errors,
            StorageStats(articles_total, articles_week, outcomes_total, db_bytes),
            last_ingest)


def _cadence_for(pipeline_id: str, cadences: Dict[str, Optional[int]]) -> Optional[int]:
    """Cadence lookup tolerant of fan-out streams (`<pipeline_id>_<variant>` envelope ids)."""
    if pipeline_id in cadences:
        return cadences[pipeline_id]
    for base, cadence in cadences.items():
        if pipeline_id.startswith(base + '_'):
            return cadence
    return None


def _fmt_bytes(size: int) -> str:
    if size >= 1024 ** 3:
        return f'{size / 1024 ** 3:.1f} GB'
    return f'{size / 1024 ** 2:.0f} MB'


def _ago(ts: Optional[datetime], until: datetime) -> str:
    if ts is None:
        return 'never'
    seconds = (until - ts).total_seconds()
    if seconds < 90:
        return f'{seconds:.0f}s ago'
    if seconds < 5400:
        return f'{seconds / 60:.0f}m ago'
    if seconds < 172800:
        return f'{seconds / 3600:.0f}h ago'
    return f'{seconds / 86400:.0f}d ago'


def _fmt_cadence(seconds: Optional[int]) -> str:
    if seconds is None:
        return '—'
    return f'{seconds}s' if seconds < 120 else f'{seconds // 60}m'


def format_weekly_report(report: WeeklyReport) -> str:
    """Console rendering — the existing section formatters stitched under one header."""
    frame = '=' * 96
    divider = '-' * 96
    sections = [
        '\n'.join([
            frame,
            'FiniexRAGEngine — Weekly Report',
            f"window: {report.since:%Y-%m-%d} → {report.until:%Y-%m-%d} (UTC)",
            frame,
        ]),
        format_cost_report(report.cost),
        format_perf_report(report.perf),
        format_source_health_report(report.sources),
        format_no_data_report(report.no_data),
        format_breaking_report(report.breaking),
        _format_storage(report, divider),
        _format_status(report, divider),
    ]
    return '\n\n'.join(sections)


def _format_storage(report: WeeklyReport, divider: str) -> str:
    s = report.storage
    return '\n'.join([
        'Storage',
        divider,
        f'corpus {s.articles_total} articles (+{s.articles_week} this week) · '
        f'envelopes {s.outcomes_total} · DB {_fmt_bytes(s.db_bytes)}',
        divider,
    ])


def _format_status(report: WeeklyReport, divider: str) -> str:
    lines = [
        'Status — workers & errors (derived from the store, no heartbeat)',
        divider,
        f'{"pipeline":32} {"cadence":>8} {"passes":>7} {"ok/part/err":>12} {"last pass":>10}',
        divider,
    ]
    for row in report.pipelines:
        verdict = '  ⚠ STALE' if row.stale else ''
        lines.append(
            f'{row.pipeline_id:32} {_fmt_cadence(row.cadence_seconds):>8} {row.passes:>7} '
            f'{f"{row.success}/{row.partial}/{row.error}":>12} '
            f'{_ago(row.last_ts, report.until):>10}{verdict}')
    if not report.pipelines:
        lines.append('(no envelopes in the store yet)')
    lines.append(f'ingest: last poll {_ago(report.last_ingest_at, report.until)}')
    if report.errors:
        lines.append('errors this week: '
                     + ' · '.join(f'{e.count} {e.type}' for e in report.errors))
    else:
        lines.append('errors this week: none')
    lines.append(divider)
    return '\n'.join(lines)
