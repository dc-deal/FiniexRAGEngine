"""Config generations (ISSUE_116) — which configuration was live on a stream, and for how long.

`config_fingerprints` says what a fingerprint stood for; this says when it ran. The distinction cost
a cross-project round trip on 2026-08-27: two fingerprints on one stream *appeared* to overlap
because the registry's `first_seen`/`last_seen` are two edge points rather than a span, and the read
that invites the error (`ORDER BY first_seen`) rendered a reverted excursion as a nested generation.

So every row here is one **activation**, with three things the registry cannot give:

- **the span it actually ran** — up to the next activation on the *same* stream, open for the
  current one;
- **what it produced in that span**, counted from the envelopes' own `config_fingerprint` stamp. A
  generation that minted nothing is the excursion class no ordering check can find;
- **why it activated** (`boot` · `reload` · `rollback`), which is what `(new)` cannot carry: the
  registry's marker is silent for a fingerprint that has been seen before, i.e. exactly on a
  rollback.

Read-only over `config_generations` + `outcomes`: no LLM, no embedding call, no write.
"""
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.utils.relative_age import format_age


@dataclass
class Activation:
    """One row of the log, as written — the input the aggregation is tested against."""
    pipeline_id: str
    fingerprint: str
    reason: str
    activated_at: datetime
    process_started_at: Optional[datetime] = None


@dataclass
class GenerationRow:
    """One activation with the span it ran and what that span produced."""
    pipeline_id: str
    fingerprint: str
    reason: str
    activated_at: datetime
    process_started_at: Optional[datetime] = None
    ran_until: Optional[datetime] = None            # None = still current on this stream
    envelopes: int = 0
    carried_in: bool = False                        # activated before the window, still current

    @property
    def current(self) -> bool:
        return self.ran_until is None

    @property
    def silent(self) -> bool:
        """Ran and produced nothing — the reverted excursion, visible only here."""
        return self.envelopes == 0


@dataclass
class GenerationsReport:
    """The activation timeline per stream, newest first."""
    since_label: str
    rows: List[GenerationRow] = field(default_factory=list)
    table_missing: bool = False                     # migration 015 has not run here

    @property
    def pipelines(self) -> List[str]:
        return sorted({row.pipeline_id for row in self.rows})

    @property
    def distinct_generations(self) -> int:
        return len({row.fingerprint for row in self.rows})

    @property
    def rollbacks(self) -> int:
        return sum(1 for row in self.rows if row.reason == 'rollback')

    def current_of(self, pipeline_id: str) -> Optional[GenerationRow]:
        return next((row for row in self.rows
                     if row.pipeline_id == pipeline_id and row.current), None)


def assign_spans(activations: Sequence[Activation],
                 stamps: Sequence[Tuple[str, datetime, Optional[str]]],
                 since: datetime) -> List[GenerationRow]:
    """The DB-free core: close each activation at the next one on its OWN stream, then count.

    Per stream rather than globally, because streams activate independently — an ingest deploy that
    restarts both pipelines writes two activations whose spans are each other's neighbours only by
    accident of ordering.

    An activation older than the window is carried in rather than dropped: it is what explains the
    envelopes at the window's start, and dropping it would leave them unattributed.
    """
    rows: List[GenerationRow] = []
    by_stream: Dict[str, List[Activation]] = {}
    for activation in sorted(activations, key=lambda item: item.activated_at):
        by_stream.setdefault(activation.pipeline_id, []).append(activation)
    for pipeline_id, entries in by_stream.items():
        for index, activation in enumerate(entries):
            following = entries[index + 1] if index + 1 < len(entries) else None
            rows.append(GenerationRow(
                pipeline_id=pipeline_id, fingerprint=activation.fingerprint,
                reason=activation.reason, activated_at=activation.activated_at,
                process_started_at=activation.process_started_at,
                ran_until=following.activated_at if following else None,
                carried_in=activation.activated_at < since))
    # One pass over the envelope stamps: each is attributed to the row whose span contains it AND
    # whose fingerprint it carries. Both conditions, never one — a stamp inside a span but carrying
    # another fingerprint is the disagreement this report exists to surface, not something to
    # silently fold into the neighbouring row.
    index_by_stream: Dict[str, List[GenerationRow]] = {}
    for row in rows:
        index_by_stream.setdefault(row.pipeline_id, []).append(row)
    for pipeline_id, ts, fingerprint in stamps:
        for row in index_by_stream.get(pipeline_id, ()):
            if row.fingerprint != fingerprint or ts < row.activated_at:
                continue
            if row.ran_until is None or ts < row.ran_until:
                row.envelopes += 1
                break
    rows.sort(key=lambda row: (row.pipeline_id, row.activated_at), reverse=True)
    return rows


def build_generations_report(database_url: str, since: datetime, *,
                             pipeline_id: Optional[str] = None,
                             since_label: str = '30d',
                             generations_table: str = 'config_generations',
                             outcomes_table: str = 'outcomes') -> GenerationsReport:
    """Read the activations in the window (plus the one carrying it in) and count what they made."""
    report = GenerationsReport(since_label=since_label)
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # A database without migration 015 says so rather than reporting an empty timeline:
            # "no activation recorded" and "this engine does not record activations" are different
            # answers, and only one of them means something is missing.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (generations_table,))
            if not cur.fetchone()[0]:
                report.table_missing = True
                return report
            where = ['activated_at >= %s']
            params: List[object] = [since]
            if pipeline_id:
                where.append('pipeline_id = %s')
                params.append(pipeline_id)
            cur.execute(f'SELECT pipeline_id, fingerprint, reason, activated_at, process_started_at '
                        f'FROM {generations_table} WHERE {" AND ".join(where)} '
                        'ORDER BY activated_at', params)
            activations = [Activation(pipeline_id=row[0], fingerprint=row[1], reason=row[2],
                                      activated_at=row[3], process_started_at=row[4])
                           for row in cur.fetchall()]
            activations += _carried_in(cur, generations_table, since, pipeline_id,
                                      {row.pipeline_id for row in activations})
            stamps = _stamps(cur, outcomes_table, since, pipeline_id)
    except psycopg.Error as exc:
        raise VectorStoreError(f'generations report failed: {exc}') from exc

    report.rows = assign_spans(activations, stamps, since)
    return report


def _carried_in(cur: psycopg.Cursor, table: str, since: datetime, pipeline_id: Optional[str],
                streams: Sequence[str]) -> List[Activation]:
    """The last activation BEFORE the window per stream — what was live when the window opened."""
    where = ['activated_at < %s']
    params: List[object] = [since]
    if pipeline_id:
        where.append('pipeline_id = %s')
        params.append(pipeline_id)
    # DISTINCT ON is the one-row-per-group read Postgres does without a window function.
    cur.execute(f'SELECT DISTINCT ON (pipeline_id) pipeline_id, fingerprint, reason, activated_at, '
                f'process_started_at FROM {table} WHERE {" AND ".join(where)} '
                'ORDER BY pipeline_id, activated_at DESC', params)
    return [Activation(pipeline_id=row[0], fingerprint=row[1], reason=row[2], activated_at=row[3],
                       process_started_at=row[4]) for row in cur.fetchall()]


def _stamps(cur: psycopg.Cursor, table: str, since: datetime,
            pipeline_id: Optional[str]) -> List[Tuple[str, datetime, Optional[str]]]:
    """Every envelope's (stream, time, fingerprint) in the window, straight from the served JSON.

    The stamp rather than a column, because that is what the consumer reads and what this report is
    about; `status <> 'error'` is deliberately NOT applied — a generation that produced only errors
    still produced, and hiding that would remove the finding.
    """
    cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s', (table,))
    if not cur.fetchone()[0]:
        return []
    where = ['ts >= %s']
    params: List[object] = [since]
    if pipeline_id:
        where.append('pipeline_id = %s')
        params.append(pipeline_id)
    cur.execute(f"SELECT pipeline_id, ts, envelope->>'config_fingerprint' FROM {table} "
                f'WHERE {" AND ".join(where)}', params)
    return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def format_generations_report(report: GenerationsReport, width: Optional[int] = None) -> str:
    """The shared console pattern: title + window line + `----` dividers + aligned columns."""
    term_width = width or shutil.get_terminal_size((100, 20)).columns
    divider = '-' * min(max(term_width - 1, 80), 110)
    lines = [f'config generations · {", ".join(report.pipelines) or "no stream"} · '
             f'{report.since_label}', divider]
    if report.table_missing:
        lines.append('this database records no activations — migration 015 has not run here')
        return '\n'.join(lines)
    if not report.rows:
        lines.append('no activation in this window (the log starts at its first boot, and cannot '
                     'recover what ran before that)')
        return '\n'.join(lines)

    lines.append(f'{"activated (UTC)":<17} {"stream":<22} {"fingerprint":<14} {"reason":<9} '
                 f'{"ran for":>8} {"envelopes":>10}')
    for row in report.rows:
        ran = ('current' if row.current
               else format_age((row.ran_until - row.activated_at).total_seconds()))
        marker = ''
        if row.silent and not row.current:
            marker = '  ⚠️ produced nothing'
        elif row.carried_in:
            marker = '  (carried into the window)'
        lines.append(f'{row.activated_at:%Y-%m-%d %H:%M}  {row.pipeline_id[:22]:<22} '
                     f'{row.fingerprint[:14]:<14} {row.reason:<9} {ran:>8} {row.envelopes:>10}'
                     f'{marker}')
    lines.append(divider)
    lines.append(f'{len(report.rows)} activation(s) · {report.distinct_generations} distinct '
                 f'generation(s) · {report.rollbacks} rollback(s)')
    for pipeline_id in report.pipelines:
        current = report.current_of(pipeline_id)
        if current:
            lines.append(f'{pipeline_id}: {current.fingerprint} live since '
                         f'{current.activated_at:%Y-%m-%d %H:%M} UTC ({current.reason})')
    lines.append('what a fingerprint STOOD for is `config_fingerprints`; this is when it ran')
    return '\n'.join(lines)
