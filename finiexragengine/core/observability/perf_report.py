"""Performance report — aggregate API-call latency from the billing log (ISSUE_32).

The exact mirror of the cost report: capture at the call (every cost_log row carries the
API call's `duration_ms` next to its tokens), report from the store. This is the latency
slice of observability (#12) — "where did the time go?" as first-class as "what did it
cost?". Rows recorded before the duration column existed are NULL and excluded from the
aggregates (counted separately, so nothing is silently dropped).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError


@dataclass
class SectionPerf:
    """One section's API latency profile inside the report window."""
    section: str
    calls: int
    avg_ms: float
    p95_ms: float
    max_ms: float
    api_seconds: float              # summed pure API time


@dataclass
class PerfReport:
    since_label: str
    rows: List[SectionPerf]         # per-section, inside the window
    untimed_calls: int              # legacy rows without duration (pre-ISSUE_32)

    @property
    def window_api_seconds(self) -> float:
        return sum(r.api_seconds for r in self.rows)


def build_perf_report(database_url: str, since: datetime, *, since_label: str = '7d',
                      table: str = 'cost_log') -> PerfReport:
    """Aggregate per-section API latency (avg/p95/max/sum) for the window."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # The read side never mutates the schema (only CostRecorder writes/upgrades
            # it). A cost_log from before the latency column — or none at all — is a
            # valid, empty answer, not a crash: without the table there is nothing to
            # report; without the column every existing row counts as untimed legacy.
            cur.execute('SELECT count(*) FROM information_schema.columns '
                        "WHERE table_name = %s AND column_name = 'duration_ms'", (table,))
            if cur.fetchone()[0] == 0:
                cur.execute('SELECT count(*) FROM information_schema.tables '
                            'WHERE table_name = %s', (table,))
                if cur.fetchone()[0] == 0:
                    return PerfReport(since_label=since_label, rows=[], untimed_calls=0)
                cur.execute(f'SELECT count(*) FROM {table} WHERE ts >= %s', (since,))
                return PerfReport(since_label=since_label, rows=[],
                                  untimed_calls=int(cur.fetchone()[0]))
            # percentile_cont is native Postgres — no extra dependency for the p95.
            cur.execute(
                f'SELECT section, count(*), avg(duration_ms), '
                f'percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), '
                f'max(duration_ms), sum(duration_ms) / 1000.0 '
                f'FROM {table} WHERE ts >= %s AND duration_ms IS NOT NULL '
                f'GROUP BY section ORDER BY sum(duration_ms) DESC', (since,))
            rows = [SectionPerf(section, int(calls), float(avg), float(p95),
                                float(mx), float(total_s))
                    for section, calls, avg, p95, mx, total_s in cur.fetchall()]
            cur.execute(f'SELECT count(*) FROM {table} '
                        f'WHERE ts >= %s AND duration_ms IS NULL', (since,))
            untimed = int(cur.fetchone()[0])
    except psycopg.Error as exc:
        raise VectorStoreError(f'performance report failed: {exc}') from exc
    return PerfReport(since_label=since_label, rows=rows, untimed_calls=untimed)


def format_perf_report(report: PerfReport) -> str:
    """Render a PerfReport as the console pattern table (the cost-report sibling)."""
    divider = '-' * 62
    lines = [
        'Performance Report',
        f'window: last {report.since_label}',
        divider,
        f'{"section":16} {"calls":>6} {"avg ms":>8} {"p95 ms":>8} {"max ms":>8} {"API s":>8}',
        divider,
    ]
    for r in report.rows:
        lines.append(f'{r.section:16} {r.calls:>6} {r.avg_ms:>8.0f} {r.p95_ms:>8.0f} '
                     f'{r.max_ms:>8.0f} {r.api_seconds:>8.1f}')
    if not report.rows:
        lines.append('(no timed API calls in the window)')
    lines.append(divider)
    lines.append(f'{"window total":16} {"":>6} {"":>8} {"":>8} {"":>8} '
                 f'{report.window_api_seconds:>8.1f}')
    if report.untimed_calls:
        lines.append('')
        lines.append(f'untimed legacy calls excluded: {report.untimed_calls} '
                     '(recorded before latency capture)')
    return '\n'.join(lines)
