"""No-data / retrieval-coverage report — symbols silently delivering nothing (ISSUE_27).

Aggregated **from the store** (persisted envelopes' `metadata.per_symbol_retrieval` +
`result[].basis`), never re-measured against the live corpus: a floor sitting above a
symbol's distance distribution produces days of mechanical `no_data` HOLDs that look like
"no news" and never raise an error. This report makes that visible — per symbol, the share
of no-data passes and how close the nearest article came to the floor. A symbol whose
nearest miss sits within a hair of the floor is flagged as a *calibration candidate*:
the floor is probably cutting real news (ISSUE_55 will auto-tune; until then the flag is
the operator's retune signal, `coverage_cli --floor` the what-if tool).
"""
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# Calibration-candidate thresholds: mostly-silent symbol AND the nearest miss within a
# hair of the floor — the cut is probably dropping real news, not noise.
_CANDIDATE_SHARE = 0.5
_CANDIDATE_MARGIN = 0.02


@dataclass
class NoDataRow:
    """One symbol's weekly no-data profile (only symbols with at least one no-data pass)."""
    pipeline_id: str
    symbol: str
    passes: int                              # envelopes carrying this symbol in the window
    no_data_passes: int                      # of those: mechanical HOLDs (basis='no_data')
    nearest_miss_min: Optional[float]        # best_distance over no-data passes (closest)
    nearest_miss_avg: Optional[float]
    floor: Optional[float]                   # latest floor snapshot seen for this symbol
    kept_avg: Optional[float]                # avg articles kept on *delivering* passes
    candidate: bool                          # calibration candidate (see thresholds above)

    @property
    def share(self) -> float:
        return self.no_data_passes / self.passes if self.passes else 0.0


@dataclass
class NoDataReport:
    since_label: str
    rows: List[NoDataRow]                    # only symbols with no-data passes, worst first
    symbols_seen: int                        # distinct (pipeline, symbol) pairs in the window

    @property
    def all_delivering(self) -> bool:
        return not self.rows


def build_no_data_report(database_url: str, since: datetime, *, since_label: str = '7d',
                         outcomes_table: str = 'outcomes') -> NoDataReport:
    """Aggregate per-symbol no-data shares from the window's persisted envelopes."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # No outcomes table yet = nothing produced; a clean empty report, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (outcomes_table,))
            if cur.fetchone()[0] == 0:
                return NoDataReport(since_label, [], 0)
            cur.execute(
                f'SELECT pipeline_id, envelope FROM {outcomes_table} '
                "WHERE ts >= %s AND status <> 'error' ORDER BY pipeline_id, ts",
                (since,))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise VectorStoreError(f'no-data report failed: {exc}') from exc

    return _aggregate(rows, since_label)


def _aggregate(rows: List[Tuple[str, object]], since_label: str) -> NoDataReport:
    """Fold envelopes into per-symbol accumulators — the DB-free core (tested)."""
    # Per (pipeline, symbol): [passes, no_data, misses, floor, kept_sum, delivering].
    passes: Dict[Tuple[str, str], int] = {}
    no_data: Dict[Tuple[str, str], int] = {}
    misses: Dict[Tuple[str, str], List[float]] = {}
    floors: Dict[Tuple[str, str], float] = {}
    kept_sum: Dict[Tuple[str, str], int] = {}
    delivering: Dict[Tuple[str, str], int] = {}

    for pipeline_id, envelope in rows:
        env = envelope if isinstance(envelope, dict) else json.loads(envelope)
        funnels = (env.get('metadata') or {}).get('per_symbol_retrieval') or {}
        for result in env.get('result', []):
            key = (pipeline_id, result['symbol'])
            passes[key] = passes.get(key, 0) + 1
            funnel = funnels.get(result['symbol']) or {}
            # Rows are ts-ordered, so overwriting leaves the *latest* floor snapshot —
            # the one a retune signal should be judged against.
            if funnel.get('floor') is not None:
                floors[key] = funnel['floor']
            if result.get('basis') == 'no_data':
                no_data[key] = no_data.get(key, 0) + 1
                # best_distance on an empty context = the nearest miss vs the floor.
                if funnel.get('best_distance') is not None:
                    misses.setdefault(key, []).append(funnel['best_distance'])
            else:
                delivering[key] = delivering.get(key, 0) + 1
                if funnel.get('kept') is not None:
                    kept_sum[key] = kept_sum.get(key, 0) + funnel['kept']

    out: List[NoDataRow] = []
    for key, total in passes.items():
        silent = no_data.get(key, 0)
        if silent == 0:
            continue   # delivering symbols make no noise — the clean line covers them
        sample = misses.get(key, [])
        miss_min = min(sample) if sample else None
        floor = floors.get(key)
        served = delivering.get(key, 0)
        candidate = (total > 0 and silent / total >= _CANDIDATE_SHARE
                     and miss_min is not None and floor is not None
                     and miss_min - floor <= _CANDIDATE_MARGIN)
        out.append(NoDataRow(
            pipeline_id=key[0], symbol=key[1], passes=total, no_data_passes=silent,
            nearest_miss_min=miss_min,
            nearest_miss_avg=sum(sample) / len(sample) if sample else None,
            floor=floor,
            kept_avg=kept_sum.get(key, 0) / served if served else None,
            candidate=candidate))

    # Worst first: candidates on top, then by silent share.
    out.sort(key=lambda row: (not row.candidate, -row.share, row.pipeline_id, row.symbol))
    return NoDataReport(since_label, out, len(passes))


def _fmt(value: Optional[float], spec: str = '.3f') -> str:
    return format(value, spec) if value is not None else '—'


def format_no_data_report(report: NoDataReport) -> str:
    """Render as the shared console pattern (aggregate — no per-run footer)."""
    divider = '-' * 96
    lines = [
        'Retrieval Coverage — no-data passes & floor calibration',
        f'window: last {report.since_label}',
        divider,
        f'{"pipeline":24} {"symbol":10} {"passes":>6} {"no-data":>8} {"share":>6} '
        f'{"miss min/avg":>14} {"floor":>6} {"kept":>5}',
        divider,
    ]
    for row in report.rows:
        flag = '  ⚠ candidate' if row.candidate else ''
        lines.append(
            f'{row.pipeline_id:24} {row.symbol:10} {row.passes:>6} {row.no_data_passes:>8} '
            f'{row.share:>6.0%} {_fmt(row.nearest_miss_min):>6}/{_fmt(row.nearest_miss_avg):>7} '
            f'{_fmt(row.floor, ".2f"):>6} {_fmt(row.kept_avg, ".1f"):>5}{flag}')
    if report.all_delivering:
        lines.append(f'all {report.symbols_seen} symbols delivering — no silent no-data')
    lines.append(divider)
    lines.append('candidate = ≥50% no-data passes AND nearest miss within 0.02 of the floor '
                 '(likely cutting real news → retune / ISSUE_55)')
    return '\n'.join(lines)
