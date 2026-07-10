"""Cost report — aggregate the billing log by section, with a spend/remaining view.

Balance is not exposed by the OpenAI API, so 'remaining' is derived: the configured
account credit (what you topped up) minus the tracked cumulative spend.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError


@dataclass
class SectionCost:
    """One section's spend inside the report window."""
    section: str
    calls: int
    total_tokens: int
    usd: float


@dataclass
class CostReport:
    since_label: str
    rows: List[SectionCost]         # per-section, inside the window
    window_usd: float               # sum over the window
    window_tokens: int
    spent_all_usd: float            # cumulative all-time spend
    credit_usd: float               # configured account credit (0 = not set)
    budget_usd: float               # configured soft window cap (0 = off)

    @property
    def remaining_usd(self) -> float:
        """Derived balance: credit − all-time spend (only meaningful when credit is set)."""
        return self.credit_usd - self.spent_all_usd


def build_cost_report(database_url: str, since: datetime, *, credit_usd: float = 0.0,
                      budget_usd: float = 0.0, since_label: str = '7d',
                      table: str = 'cost_log') -> CostReport:
    """Aggregate the cost log by section for the window, plus the all-time total."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # Read side only — a fresh DB where no CostRecorder ever created the table
            # is a valid 'nothing spent yet' answer, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables '
                        'WHERE table_name = %s', (table,))
            if cur.fetchone()[0] == 0:
                return CostReport(since_label=since_label, rows=[], window_usd=0.0,
                                  window_tokens=0, spent_all_usd=0.0,
                                  credit_usd=credit_usd, budget_usd=budget_usd)
            cur.execute(
                f'SELECT section, count(*), coalesce(sum(total_tokens), 0), '
                f'coalesce(sum(usd_cost), 0) FROM {table} WHERE ts >= %s '
                f'GROUP BY section ORDER BY sum(usd_cost) DESC', (since,))
            rows = [SectionCost(section, int(calls), int(tokens), float(usd))
                    for section, calls, tokens, usd in cur.fetchall()]
            cur.execute(f'SELECT coalesce(sum(usd_cost), 0) FROM {table}')
            spent_all = float(cur.fetchone()[0])
    except psycopg.Error as exc:
        raise VectorStoreError(f'cost report failed: {exc}') from exc
    return CostReport(
        since_label=since_label, rows=rows,
        window_usd=sum(r.usd for r in rows), window_tokens=sum(r.total_tokens for r in rows),
        spent_all_usd=spent_all, credit_usd=credit_usd, budget_usd=budget_usd)


def format_cost_report(report: CostReport) -> str:
    """Render a CostReport as a console table with the derived-balance line."""
    divider = '-' * 54
    # 6 decimals: embedding spend is fractions of a cent, so 2–4 dp would read $0.0000.
    lines = [
        'Cost Report',
        f'window: last {report.since_label}',
        divider,
        f'{"section":16} {"calls":>6} {"tokens":>10} {"USD":>13}',
        divider,
    ]
    for r in report.rows:
        lines.append(f'{r.section:16} {r.calls:>6} {r.total_tokens:>10,} {r.usd:>13.6f}')
    if not report.rows:
        lines.append('(no paid calls in the window)')
    lines.append(divider)
    lines.append(f'{"window total":16} {"":>6} {report.window_tokens:>10,} {report.window_usd:>13.6f}')
    lines.append('')
    lines.append(f'spent (all-time): ${report.spent_all_usd:.6f}')
    if report.credit_usd > 0:
        lines.append(f'account credit:   ${report.credit_usd:.2f}  →  remaining ≈ '
                     f'${report.remaining_usd:.4f}')
    else:
        lines.append('account credit:   not set (set cost.account_credit_usd to see remaining)')
    if report.budget_usd > 0:
        over = '  ⚠️ OVER' if report.window_usd > report.budget_usd else ''
        lines.append(f'budget (window):  ${report.budget_usd:.2f}{over}')
    return '\n'.join(lines)
