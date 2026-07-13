"""Cost report — REAL spend (billing log) + a config-driven spend PREDICTION (ISSUE_23/12).

Two clearly separated parts, so real and estimated numbers are never confused:

- **REAL** — aggregated from the billing log (`cost_log`): actual USD, windowed (this week /
  this month / all-time) and split per pipeline / source-set. Ground truth, frozen at record time.
- **PREDICTION** — *extrapolated*: the **real** measured cost per eval pass (from the persisted
  envelopes) × the **current effective config's** eval cadence. Every projected figure is marked
  (⚠️ + "est" / "~"); these are estimates of a continuous run, **not** real consumption.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError

_DAY_SECONDS = 86400.0


@dataclass
class LineItem:
    """One pipeline / source-set's real spend inside a window."""
    label: str
    calls: int
    tokens: int
    usd: float


@dataclass
class RealWindow:
    """Actual billing-log spend over one time window."""
    label: str
    calls: int
    tokens: int
    usd: float
    by_pipeline: List[LineItem] = field(default_factory=list)


@dataclass
class EvalPipelineInfo:
    """What the projection needs to know about one eval pipeline (from the effective config)."""
    interval_seconds: int
    symbol_count: int
    overridden: bool               # True when a gitignored user override is deep-merged in


@dataclass
class PipelineProjection:
    """One pipeline's projected eval cost — real $/pass × config cadence (EXTRAPOLATED)."""
    pipeline_id: str
    usd_per_pass: float            # REAL: avg persisted envelope cost over recent passes
    passes_per_day: float          # CONFIG: 86400 / eval_interval (effective config)
    usd_per_day: float             # derived (EXTRAPOLATED)
    symbol_count: int = 0          # symbols this pipeline evaluates (effective config)
    overridden: bool = False       # a user override is active for this stream's constellation

    @property
    def usd_per_week(self) -> float:
        return self.usd_per_day * 7.0

    @property
    def usd_per_month(self) -> float:
        return self.usd_per_day * 30.0


@dataclass
class Prediction:
    """Config-driven extrapolation — explicitly NOT real spend."""
    per_pipeline: List[PipelineProjection]
    usd_per_day: float
    usd_per_week: float
    usd_per_month: float
    sampled_passes: int            # how many real passes the per-pass averages drew on


@dataclass
class CostReport:
    real: List[RealWindow]                    # this week, this month, all-time
    prediction: Optional[Prediction]
    spent_all_usd: float
    credit_usd: float = 0.0                    # configured account credit (0 = not set)

    @property
    def remaining_usd(self) -> float:
        return self.credit_usd - self.spent_all_usd


def _table_exists(cur, table: str) -> bool:
    cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s', (table,))
    return cur.fetchone()[0] > 0


def _window(cur, table: str, label: str, since: Optional[datetime]) -> RealWindow:
    """Aggregate the billing log for one window: totals + a per-pipeline breakdown."""
    where, params = ('', [])
    if since is not None:
        where, params = ('WHERE ts >= %s', [since])
    cur.execute(f'SELECT count(*), coalesce(sum(total_tokens), 0), coalesce(sum(usd_cost), 0) '
                f'FROM {table} {where}', params)
    calls, tokens, usd = cur.fetchone()
    cur.execute(
        f"SELECT coalesce(pipeline_id, '(unattributed)'), count(*), "
        f'coalesce(sum(total_tokens), 0), coalesce(sum(usd_cost), 0) '
        f'FROM {table} {where} GROUP BY pipeline_id ORDER BY sum(usd_cost) DESC', params)
    by_pipeline = [LineItem(pid, int(c), int(t), float(u)) for pid, c, t, u in cur.fetchall()]
    return RealWindow(label, int(calls), int(tokens), float(usd), by_pipeline)


def _build_prediction(cur, outcomes_table: str,
                      eval_pipelines: Dict[str, 'EvalPipelineInfo'],
                      recent_passes: int) -> Optional[Prediction]:
    """Project daily/weekly/monthly eval cost from the real recent $/pass × the config cadence.

    `eval_pipelines` = {pipeline_id: EvalPipelineInfo}, from the **effective** config (base +
    user override) — so the projection reflects what actually runs, and carries the symbol count
    and override flag that explain each pipeline's per-pass cost. The per-pass cost is read from
    the persisted envelopes (real, recent); the forward projection is the extrapolation.
    """
    if not eval_pipelines or not _table_exists(cur, outcomes_table):
        return None
    projections: List[PipelineProjection] = []
    sampled = 0
    for pipeline_id, info in sorted(eval_pipelines.items()):
        if not info.interval_seconds:
            continue
        # Real average cost of the most recent passes for this stream (error passes excluded).
        cur.execute(
            f"SELECT avg(c), count(*) FROM (SELECT (envelope->'metadata'->>'cost_usd')::float AS c "
            f'FROM {outcomes_table} WHERE pipeline_id = %s AND status <> %s '
            f'ORDER BY ts DESC LIMIT %s) recent',
            (pipeline_id, 'error', recent_passes))
        avg_usd, n = cur.fetchone()
        if not n:
            continue                              # no real passes yet → cannot ground a projection
        usd_per_pass = float(avg_usd or 0.0)
        passes_per_day = _DAY_SECONDS / info.interval_seconds
        projections.append(PipelineProjection(
            pipeline_id, usd_per_pass, passes_per_day, usd_per_pass * passes_per_day,
            symbol_count=info.symbol_count, overridden=info.overridden))
        sampled += int(n)
    if not projections:
        return None
    per_day = sum(p.usd_per_day for p in projections)
    return Prediction(projections, per_day, per_day * 7.0, per_day * 30.0, sampled)


def build_cost_report(database_url: str, *,
                      eval_pipelines: Optional[Dict[str, EvalPipelineInfo]] = None,
                      credit_usd: float = 0.0, recent_passes: int = 20,
                      cost_table: str = 'cost_log',
                      outcomes_table: str = 'outcomes') -> CostReport:
    """Assemble the real-spend windows + the config-driven prediction."""
    now = datetime.now(timezone.utc)
    windows = [('this week', now - timedelta(days=7)),
               ('this month', now - timedelta(days=30)),
               ('all-time', None)]
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # A fresh DB where no CostRecorder ever created the table = 'nothing spent yet'.
            if not _table_exists(cur, cost_table):
                empty = [RealWindow(label, 0, 0, 0.0) for label, _ in windows]
                return CostReport(empty, None, 0.0, credit_usd)
            real = [_window(cur, cost_table, label, since) for label, since in windows]
            spent_all = next(w.usd for w in real if w.label == 'all-time')
            prediction = _build_prediction(cur, outcomes_table, eval_pipelines or {},
                                           recent_passes)
    except psycopg.Error as exc:
        raise VectorStoreError(f'cost report failed: {exc}') from exc
    return CostReport(real, prediction, spent_all, credit_usd)


def _fmt_usd(usd: float) -> str:
    # 6 decimals: embedding spend is fractions of a cent, so 2–4 dp would read $0.0000.
    return f'{usd:.6f}'


def format_cost_report(report: CostReport) -> str:
    """Render REAL (part A) then PREDICTION (part B), with the estimate clearly marked."""
    wide = '-' * 60
    wide_pred = '-' * 99
    lines = ['Cost Report', '', '=== REAL spend (billing log — actual USD) ' + '=' * 18,
             f'{"window":12} {"calls":>6} {"tokens":>11} {"USD":>14}', wide]
    for window in report.real:
        lines.append(f'{window.label:12} {window.calls:>6} {window.tokens:>11,} '
                     f'{_fmt_usd(window.usd):>14}')
    # Per-pipeline / source-set attribution over the full history (the complete picture).
    all_time = next((w for w in report.real if w.label == 'all-time'), None)
    if all_time and all_time.by_pipeline:
        lines += ['', 'by pipeline / source-set (all-time):']
        for item in all_time.by_pipeline:
            lines.append(f'  {item.label:30} {item.calls:>6} {item.tokens:>11,} '
                         f'{_fmt_usd(item.usd):>14}')

    lines += ['', '=== PREDICTION  ⚠️ EXTRAPOLATED — NOT REAL SPEND ' + '=' * 11]
    prediction = report.prediction
    if prediction is None:
        lines.append('(no real passes yet to project from — run the workers first)')
    else:
        lines += [
            'Estimated from the REAL avg $/eval-pass (measured, '
            f'{prediction.sampled_passes} recent passes) × the current effective config cadence.',
            'Excludes breaking wakes (variable — add more) and assumes a continuous run.',
            'The $/pass is measured from the most recent passes — it converges after a config '
            'change (e.g. a symbol-count / model override) actually runs.',
            '',
            f'{"pipeline":30} {"sym":>3} {"ovr":>3} {"$/pass(real)":>13} {"passes/d":>9} '
            f'{"$/day(est)":>11} {"$/week(est)":>11} {"$/month(est)":>12}', wide_pred]
        for proj in prediction.per_pipeline:
            lines.append(f'{proj.pipeline_id:30} {proj.symbol_count:>3} '
                         f'{("yes" if proj.overridden else "-"):>3} {proj.usd_per_pass:>13.6f} '
                         f'{proj.passes_per_day:>9.0f} {proj.usd_per_day:>11.4f} '
                         f'{proj.usd_per_week:>11.2f} {proj.usd_per_month:>12.2f}')
        lines += [
            wide_pred,
            f'projected total (EXTRAPOLATED):  ~${prediction.usd_per_day:.4f}/day   '
            f'~${prediction.usd_per_week:.2f}/week   ~${prediction.usd_per_month:.2f}/month',
            '⚠️  these are extrapolated estimates, not real consumption.']

    lines += ['', f'spent (all-time, real): ${_fmt_usd(report.spent_all_usd)}']
    if report.credit_usd > 0:
        lines.append(f'account credit:         ${report.credit_usd:.2f}  →  remaining ≈ '
                     f'${report.remaining_usd:.4f}')
    return '\n'.join(lines)
