"""Source-health report (ISSUE_11) — feed reliability + the debugging-ready problem log.

Reads the `source_health` rows the ingest worker captured (CLAUDE.md — report from the store)
and renders the shared console pattern: per-feed poll counts / success rate / flag+quarantine
state, a capped list of the most recent warnings/errors (so the operator debugs a feed without
digging through logs), and an **orphan notice** for sources still in the store but no longer in
any current config (`may be deleted`). The same aggregation feeds the weekly report (#27).
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Set

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# The debugging-ready problem log is capped for overview (operator: "max 10").
_RECENT_PROBLEMS = 10


@dataclass
class SourceHealthRow:
    """One feed's rolling health, as read from source_health."""
    source_id: str
    host: str
    source_set: str
    total_polls: int
    total_success: int
    total_failures: int
    consecutive_failures: int
    last_success_at: Optional[datetime]
    last_failure_at: Optional[datetime]
    last_status: Optional[int]
    last_error_type: Optional[str]
    flagged: bool
    quarantined_until: Optional[datetime]
    recent_events: List[dict] = field(default_factory=list)

    @property
    def success_rate(self) -> Optional[float]:
        return self.total_success / self.total_polls if self.total_polls else None

    @property
    def quarantined(self) -> bool:
        return bool(self.quarantined_until
                    and self.quarantined_until > datetime.now(timezone.utc))


@dataclass
class SourceHealthReport:
    rows: List[SourceHealthRow]
    orphans: List[str]        # source_ids in the store but not in any current config

    @property
    def flagged_count(self) -> int:
        return sum(1 for row in self.rows if row.flagged)


def build_source_health_report(database_url: str, configured_ids: Set[str], *,
                               table: str = 'source_health') -> SourceHealthReport:
    """Load the health rows and mark orphans against the currently-configured source ids."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (table,))
            if cur.fetchone()[0] == 0:
                return SourceHealthReport([], [])
            cur.execute(
                f'SELECT source_id, host, source_set, total_polls, total_success, '
                'total_failures, consecutive_failures, last_success_at, last_failure_at, '
                'last_status, last_error_type, flagged, quarantined_until, recent_events '
                f'FROM {table} ORDER BY source_id')
            rows = [SourceHealthRow(*row[:13], recent_events=list(row[13] or []))
                    for row in cur.fetchall()]
    except psycopg.Error as exc:
        raise VectorStoreError(f'source health report failed: {exc}') from exc

    orphans = sorted(row.source_id for row in rows if row.source_id not in configured_ids)
    return SourceHealthReport(rows, orphans)


def _ago(moment: Optional[datetime]) -> str:
    """Compact age of a timestamp ('12s', '3h', '2d', or '—')."""
    if moment is None:
        return '—'
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 90:
        return f'{seconds:.0f}s'
    if seconds < 5400:
        return f'{seconds / 60:.0f}m'
    if seconds < 172800:
        return f'{seconds / 3600:.0f}h'
    return f'{seconds / 86400:.0f}d'


def _remaining(moment: Optional[datetime]) -> str:
    """Compact time left until a future moment ('21h', '35m', or '0m')."""
    if moment is None:
        return '0m'
    minutes = max(0.0, (moment - datetime.now(timezone.utc)).total_seconds()) / 60
    return f'{minutes / 60:.0f}h' if minutes >= 90 else f'{minutes:.0f}m'


def _status_cell(row: SourceHealthRow) -> str:
    if row.flagged:
        detail = row.last_error_type or 'error'
        if row.quarantined:
            return f'FLAGGED({detail}) quarantined {_remaining(row.quarantined_until)} left'
        return f'FLAGGED({detail}) retrying'
    if row.consecutive_failures:
        return f'failing ({row.last_error_type or "error"})'
    return 'ok'


def _recent_problems(rows: Sequence[SourceHealthRow]) -> List[str]:
    """Newest warnings/errors across all feeds, capped for overview."""
    events = []
    for row in rows:
        for event in row.recent_events:
            events.append((event.get('ts', ''), row.source_id, event))
    events.sort(key=lambda item: item[0], reverse=True)
    lines = []
    for ts, source_id, event in events[:_RECENT_PROBLEMS]:
        when = ts.replace('T', ' ')[5:16] if ts else '—'          # MM-DD HH:MM
        status = f"({event.get('status')})" if event.get('status') is not None else ''
        lines.append(f"  [{source_id}] {when} {event.get('level', '?')} "
                     f"{event.get('type', '?')}{status}: {event.get('message', '')}")
    return lines


def format_source_health_report(report: SourceHealthReport) -> str:
    """Render the report as the shared console pattern (title + window line + dividers)."""
    divider = '-' * 88
    quarantined = sum(1 for row in report.rows if row.quarantined)
    lines = [
        'Source Health — feeds & problems',
        f'sources: {len(report.rows)} tracked · {report.flagged_count} flagged · '
        f'{quarantined} quarantined · {len(report.orphans)} orphaned',
        divider,
        f'{"source":18} {"host":22} {"polls":>7} {"ok%":>5} {"consec":>6} '
        f'{"last ok":>8}  status',
        divider,
    ]
    for row in report.rows:
        rate = f'{row.success_rate * 100:.0f}%' if row.success_rate is not None else '—'
        consec = f'{row.consecutive_failures}' + ('!' if row.flagged else '')
        lines.append(f'{row.source_id:18.18} {row.host:22.22} {row.total_polls:>7} '
                     f'{rate:>5} {consec:>6} {_ago(row.last_success_at):>8}  {_status_cell(row)}')
    if not report.rows:
        lines.append('(no source health captured yet — run the ingest workers)')
    lines.append(divider)

    problems = _recent_problems(report.rows)
    lines.append(f'recent problems (last {_RECENT_PROBLEMS}):')
    lines.extend(problems if problems else ['  (none)'])
    lines.append(divider)
    lines.append('orphaned (in the health store, not in any current config — may be deleted):')
    lines.extend([f'  {sid}' for sid in report.orphans] if report.orphans else ['  (none)'])
    return '\n'.join(lines)
