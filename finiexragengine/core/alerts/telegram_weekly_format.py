"""Telegram rendering of the weekly model (ISSUE_27) — compact HTML, section-packed.

Reads only the typed `WeeklyReport` (same model the console renders — one truth, two
surfaces). Telegram is a proportional-font, 4096-chars-per-message medium, so this is a
deliberate divergence from the console pattern table: short labelled lines, bold section
heads, no aligned columns. Sections are packed greedily into as few messages as fit;
splits happen only at section boundaries.
"""
import html
from datetime import datetime
from typing import List, Optional

from finiexragengine.core.observability.reports.no_data_report import NoDataReport
from finiexragengine.core.observability.reports.weekly_report import WeeklyReport

# Pack limit deliberately below Telegram's 4096 hard cap — headroom for HTML entities.
_PACK_LIMIT = 3500


def render_weekly_messages(report: WeeklyReport) -> List[str]:
    """The weekly as 1..n Telegram messages (usually one), ready for TelegramClient."""
    sections = [
        _header(report),
        _cost(report),
        _perf(report),
        _sources(report),
        _no_data(report.no_data),
        _breaking(report),
        _storage(report),
        _status(report),
    ]
    return _pack(sections)


def _header(report: WeeklyReport) -> str:
    return ('📊 <b>FiniexRAGEngine — Weekly Report</b>\n'
            f"{report.since:%Y-%m-%d} → {report.until:%Y-%m-%d} (UTC)")


def _cost(report: WeeklyReport) -> str:
    lines = ['💰 <b>Cost</b>']
    for window in report.cost.real:
        lines.append(f'{window.label}: {window.calls} calls · {_tokens(window.tokens)} tok '
                     f'· ${window.usd:.4f}')
    if report.cost.prediction is not None:
        lines.append(f'projected ~${report.cost.prediction.usd_per_week:.2f}/week (est)')
    if report.cost.credit_usd > 0:
        lines.append(f'credit ${report.cost.credit_usd:.2f} → '
                     f'${report.cost.remaining_usd:.4f} remaining')
    return '\n'.join(lines)


def _perf(report: WeeklyReport) -> str:
    lines = [f'⚡ <b>Performance</b> ({report.perf.since_label})']
    for row in report.perf.rows:
        lines.append(f'{html.escape(row.section)}: {row.calls} calls · '
                     f'avg {_ms(row.avg_ms)} · p95 {_ms(row.p95_ms)}')
    if not report.perf.rows:
        lines.append('no timed calls in the window')
    return '\n'.join(lines)


def _sources(report: WeeklyReport) -> str:
    health = report.sources
    problems = [row for row in health.rows if (row.flagged or row.quarantined)
                and not row.disabled]
    # Early warning: not (yet) flagged, but failed inside the window.
    failing = [row for row in health.rows
               if row not in problems and not row.disabled
               and row.last_failure_at is not None and row.last_failure_at >= report.since]
    lines = ['📡 <b>Sources</b>'
             + (f' — {len(problems)} flagged' if problems else '')]
    for row in problems:
        state = html.escape(row.last_error_type or 'failing')
        quarantine = (f', quarantined {_until(row.quarantined_until, report.until)}'
                      if row.quarantined else '')
        lines.append(f'⚠ {html.escape(row.source_id)}: {state}, '
                     f'{row.consecutive_failures}× consecutive{quarantine}')
    for row in failing:
        rate = f'{row.success_rate:.0%}' if row.success_rate is not None else '—'
        lines.append(f'{html.escape(row.source_id)}: failed {_ago(row.last_failure_at, report.until)}'
                     f' · {rate} ok')
    if health.orphans:
        lines.append('orphan: ' + ', '.join(html.escape(o) for o in health.orphans)
                     + ' (may be deleted)')
    if len(lines) == 1:
        lines.append('all feeds healthy')
    return '\n'.join(lines)


def _no_data(no_data: NoDataReport) -> str:
    lines = ['🔍 <b>Retrieval coverage</b>']
    many_pipelines = len({row.pipeline_id for row in no_data.rows}) > 1
    for row in no_data.rows:
        prefix = f'{html.escape(row.pipeline_id)} · ' if many_pipelines else ''
        miss = f' · miss {row.nearest_miss_min:.3f}' if row.nearest_miss_min is not None else ''
        floor = f' vs floor {row.floor:.2f}' if row.floor is not None else ''
        flag = ' ⚠ candidate' if row.candidate else ''
        lines.append(f'{prefix}{html.escape(row.symbol)} {row.share:.0%} no-data'
                     f'{miss}{floor}{flag}')
    if no_data.all_delivering:
        lines.append(f'all {no_data.symbols_seen} symbols delivering')
    return '\n'.join(lines)


def _breaking(report: WeeklyReport) -> str:
    lines = ['🚨 <b>Breaking</b>']
    for row in report.breaking.rows:
        lines.append(f'{html.escape(row.pipeline_id)}: {row.confirmed} confirmed')
    lines.append(f'funnel: {report.breaking.flagged_candidates} flagged → '
                 f'{report.breaking.confirmed_episodes} confirmed')
    return '\n'.join(lines)


def _storage(report: WeeklyReport) -> str:
    storage = report.storage
    return ('🗄 <b>Storage</b>\n'
            f'corpus {storage.articles_total} articles (+{storage.articles_week} wk) · '
            f'envelopes {storage.outcomes_total} · DB {_bytes(storage.db_bytes)}')


def _status(report: WeeklyReport) -> str:
    lines = ['⚙️ <b>Status</b>']
    for row in report.pipelines:
        stale = ' ⚠ STALE' if row.stale else ''
        lines.append(f'{html.escape(row.pipeline_id)}: last pass '
                     f'{_ago(row.last_ts, report.until)} · '
                     f'{row.success}/{row.partial}/{row.error} ok/part/err{stale}')
    if not report.pipelines:
        lines.append('no envelopes in the store yet')
    lines.append(f'ingest: last poll {_ago(report.last_ingest_at, report.until)}')
    if report.errors:
        lines.append('errors: ' + ' · '.join(
            f'{e.count} {html.escape(e.type)}' for e in report.errors))
    else:
        lines.append('errors: none')
    return '\n'.join(lines)


def _pack(sections: List[str]) -> List[str]:
    """Greedy section packing — a split never lands inside a section."""
    messages: List[str] = []
    current = ''
    for section in sections:
        candidate = section if not current else f'{current}\n\n{section}'
        if current and len(candidate) > _PACK_LIMIT:
            messages.append(current)
            current = section
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def _tokens(count: int) -> str:
    return f'{count / 1000:.0f}k' if count >= 10_000 else str(count)


def _ms(value: float) -> str:
    return f'{value:.0f}ms' if value < 1000 else f'{value / 1000:.1f}s'


def _bytes(size: int) -> str:
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


def _until(ts: Optional[datetime], now: datetime) -> str:
    if ts is None:
        return '—'
    seconds = (ts - now).total_seconds()
    if seconds <= 0:
        return 'ending'
    if seconds < 5400:
        return f'{seconds / 60:.0f}m left'
    return f'{seconds / 3600:.0f}h left'
