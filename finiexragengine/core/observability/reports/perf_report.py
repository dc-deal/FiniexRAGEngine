"""Performance report — aggregate API-call latency from the billing log (ISSUE_32).

The exact mirror of the cost report: capture at the call (every cost_log row carries the
API call's `duration_ms` next to its tokens), report from the store. This is the latency
slice of observability (#12) — "where did the time go?" as first-class as "what did it
cost?". Rows recorded before the duration column existed are NULL and excluded from the
aggregates (counted separately, so nothing is silently dropped).

Since ISSUE_128 a row is one *attempt*: latency is aggregated over the attempts that answered
(`outcome = 'ok'`), and a second block counts per pipeline × model how many calls needed a retry
and how the failed attempts ended. Before the migration a row's `duration_ms` included any retries
the SDK made out of sight — the latency series changes meaning at that boundary.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Tuple

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
class AttemptRow:
    """One pipeline × model's attempts inside the window (ISSUE_128)."""
    label: str                      # pipeline / source set ('(unattributed)' without one)
    model: str
    calls: int                      # attempts that answered = calls that succeeded
    retried: int                    # of those, answered on a later attempt than the first
    failed: Dict[str, int]          # failed attempts by outcome ('timeout': 2, ...)
    spend_unknown: int              # failed attempts whose spend is unknown (NULL USD)

    @property
    def failed_total(self) -> int:
        return sum(self.failed.values())


@dataclass
class PerfReport:
    since_label: str
    rows: List[SectionPerf]         # per-section, inside the window
    untimed_calls: int              # legacy rows without duration (pre-ISSUE_32)
    attempts: List[AttemptRow] = field(default_factory=list)
    # False on a billing log from before migration 019: attempts were not recorded, so the block
    # says so instead of reading as "nothing failed".
    attempts_recorded: bool = True

    @property
    def window_api_seconds(self) -> float:
        return sum(r.api_seconds for r in self.rows)


def _column_exists(cur: psycopg.Cursor, table: str, column: str) -> bool:
    cur.execute('SELECT count(*) FROM information_schema.columns '
                'WHERE table_name = %s AND column_name = %s', (table, column))
    return cur.fetchone()[0] > 0


def _attempt_rows(cur: psycopg.Cursor, table: str, since: datetime) -> List[AttemptRow]:
    """Per pipeline × model: successful calls, how many were retried, failures by outcome."""
    cur.execute(
        f"SELECT coalesce(pipeline_id, '(unattributed)'), model, "
        f"count(*) FILTER (WHERE outcome = 'ok'), "
        f"count(*) FILTER (WHERE outcome = 'ok' AND attempt > 1), "
        f"count(*) FILTER (WHERE outcome <> 'ok' AND usd_cost IS NULL) "
        f'FROM {table} WHERE ts >= %s GROUP BY 1, 2', (since,))
    totals = {(label, model): (int(ok), int(retried), int(unknown))
              for label, model, ok, retried, unknown in cur.fetchall()}
    cur.execute(
        f"SELECT coalesce(pipeline_id, '(unattributed)'), model, outcome, count(*) "
        f"FROM {table} WHERE ts >= %s AND outcome <> 'ok' GROUP BY 1, 2, 3", (since,))
    failed: Dict[Tuple[str, str], Dict[str, int]] = {}
    for label, model, outcome, count in cur.fetchall():
        failed.setdefault((label, model), {})[outcome] = int(count)
    rows = [AttemptRow(label, model, ok, retried, dict(sorted(failed.get((label, model),
                                                                         {}).items())), unknown)
            for (label, model), (ok, retried, unknown) in totals.items()]
    # The rows that need attention first, then by volume.
    return sorted(rows, key=lambda row: (-row.failed_total, -row.retried, -row.calls, row.label,
                                         row.model))


def build_perf_report(database_url: str, since: datetime, *, since_label: str = '7d',
                      table: str = 'cost_log') -> PerfReport:
    """Aggregate per-section API latency (avg/p95/max/sum) for the window."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # The read side never mutates the schema (only CostRecorder writes/upgrades
            # it). A cost_log from before the latency column — or none at all — is a
            # valid, empty answer, not a crash: without the table there is nothing to
            # report; without the column every existing row counts as untimed legacy.
            if not _column_exists(cur, table, 'duration_ms'):
                cur.execute('SELECT count(*) FROM information_schema.tables '
                            'WHERE table_name = %s', (table,))
                if cur.fetchone()[0] == 0:
                    return PerfReport(since_label=since_label, rows=[], untimed_calls=0)
                cur.execute(f'SELECT count(*) FROM {table} WHERE ts >= %s', (since,))
                return PerfReport(since_label=since_label, rows=[],
                                  untimed_calls=int(cur.fetchone()[0]), attempts_recorded=False)
            # Latency is a property of the calls that answered: a timed-out attempt's duration is
            # our deadline, not the provider's speed (ISSUE_128). A table from before migration
            # 019 holds only answered calls, so it needs no filter.
            recorded = _column_exists(cur, table, 'outcome')
            answered = " AND outcome = 'ok'" if recorded else ''
            # percentile_cont is native Postgres — no extra dependency for the p95.
            cur.execute(
                f'SELECT section, count(*), avg(duration_ms), '
                f'percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms), '
                f'max(duration_ms), sum(duration_ms) / 1000.0 '
                f'FROM {table} WHERE ts >= %s AND duration_ms IS NOT NULL{answered} '
                f'GROUP BY section ORDER BY sum(duration_ms) DESC', (since,))
            rows = [SectionPerf(section, int(calls), float(avg), float(p95),
                                float(mx), float(total_s))
                    for section, calls, avg, p95, mx, total_s in cur.fetchall()]
            cur.execute(f'SELECT count(*) FROM {table} '
                        f'WHERE ts >= %s AND duration_ms IS NULL', (since,))
            untimed = int(cur.fetchone()[0])
            attempts = _attempt_rows(cur, table, since) if recorded else []
    except psycopg.Error as exc:
        raise VectorStoreError(f'performance report failed: {exc}') from exc
    return PerfReport(since_label=since_label, rows=rows, untimed_calls=untimed,
                      attempts=attempts, attempts_recorded=recorded)


def _format_attempts(report: PerfReport) -> List[str]:
    """The attempts block (ISSUE_128): which calls needed a retry, and how failures ended."""
    # As wide as the longest `pipeline · model` — variant streams and embedding model ids together
    # run past any fixed width that would still read well for the short ones.
    labels = [f'{row.label} · {row.model}' for row in report.attempts]
    width = max([len('pipeline · model')] + [len(label) for label in labels]) + 2
    divider = '-' * (width + 62)
    lines = ['', f'LLM / embedding attempts (last {report.since_label})', divider,
             f'{"pipeline · model":{width}} {"calls":>7} {"retried":>8}  {"failed attempts":30} '
             f'{"spend unknown":>13}', divider]
    if not report.attempts_recorded:
        lines.append('(not recorded — the billing log predates per-attempt rows, migration 019)')
        return lines
    for row, label in zip(report.attempts, labels):
        failed = ' · '.join(f'{count} {outcome}' for outcome, count in row.failed.items()) or '-'
        unknown = str(row.spend_unknown) if row.spend_unknown else '-'
        lines.append(f'{label:{width}} {row.calls:>7,} {row.retried:>8,}  '
                     f'{failed:30} {unknown:>13}')
    if not report.attempts:
        lines.append('(no API calls in the window)')
    lines.append(divider)
    retried = sum(row.retried for row in report.attempts)
    failed_total = sum(row.failed_total for row in report.attempts)
    unknown_total = sum(row.spend_unknown for row in report.attempts)
    lines.append(f'{retried} call(s) retried · {failed_total} failed attempt(s), '
                 f'{unknown_total} with unknown spend')
    return lines


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
    lines += _format_attempts(report)
    return '\n'.join(lines)
