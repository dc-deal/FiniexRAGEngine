"""Retrieval drift — did the evidence reaching the prompt move when the setup changed (ISSUE_55).

`prompt_drift` answers whether the *answers* moved across a configuration change. Nothing answered
whether the *evidence those answers were formed from* moved, and on 2026-09-01 that gap cost two
wrong diagnoses before a hand-written query settled it. This report is that query, rendered.

The measurements are not new: `metadata.per_symbol_retrieval` has carried the funnel per symbol since
ISSUE_24 — `in_window`, `floor_dropped`, `kept`, `deep_kept`, `best_distance` and the `floor` that
was actually applied. Reading them per `config_fingerprint` turns six weeks of archive into a
before/after that needs no experiment.

**The weekday is part of the key, and that is the whole point.** The ISSUE_112 normaliser deployed on
a Saturday. Read straight off the day series it looked like a 42 % collapse in delivered evidence;
against matched weekdays it is 18 %, because Sunday costs both pipelines around a third of their
corpus with no deploy involved. A drift surface that pooled weekdays would reproduce exactly the
error it exists to prevent, so rows are `(pipeline, fingerprint, weekday)` and the two fingerprints
for one weekday render adjacent.

**Two columns, two different findings, and only the pair separates them.** A rising `cut%` beside a
*flat* `best_distance` is the floor meeting a wider spread — the near articles stayed near, the
marginal ones drifted out. A rising `cut%` beside a *rising* `best_distance` is the corpus moving
away from the query, which is a different problem with a different fix. Either alone is ambiguous.

**What this report cannot say.** The funnel stores counters and distances, never source ids, so the
share of in-floor articles arriving from another pipeline's feeds is not derivable here. That measure
needs the corpus, which would make this a paid call and disqualify it from the catalog;
`floor_profile_cli` pays for it deliberately and keeps it.

Read-only over persisted envelopes: no corpus access, no query-vector cache, no paid call.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# ISO weekday (1=Monday) -> the label the console prints. Ordering by the number rather than the
# label keeps Monday first instead of Friday, which an alphabetical sort would produce.
_WEEKDAYS: Tuple[str, ...] = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

# Below these a step is noise rather than a finding, and `reading` says "steady" / "stayed put"
# instead of naming a cause. The distance band is the tighter judgement: production steps that
# matter run 0.02–0.07, while a same-configuration weekday wobbles by 0.001–0.006.
_CUT_FLAT: float = 2.0            # percentage points
_DISTANCE_FLAT: float = 0.01      # cosine distance
# Above this relative change the candidate pool is a different basis and the rate columns stop
# comparing — see `RetrievalDriftDelta.basis_changed` for the measurement behind the number.
_POOL_RESHAPED: float = 0.5


@dataclass
class RetrievalDriftRow:
    """One (pipeline, config fingerprint, weekday) cell of the retrieval funnel."""
    pipeline_id: str
    config_fingerprint: str
    weekday: int                             # ISO: 1 = Monday … 7 = Sunday
    symbol_passes: int                       # funnel entries, i.e. symbol-passes, not envelopes
    first_seen: Optional[datetime]
    last_seen: Optional[datetime]
    candidates_sum: int                      # sum of `in_window` over the cell
    floor_dropped_sum: int
    kept_sum: int
    deep_kept_sum: int
    best_distance_sum: float
    best_distance_count: int                 # separate: `best_distance` is null on an empty window
    # Every distinct floor snapshot seen in the cell. More than one means the floor was retuned
    # inside it — visible from the archive instead of inferred from config history.
    floors: Tuple[float, ...] = ()
    # Informational, not part of the key: `config_fingerprint` is the finer discriminator (ISSUE_112
    # measured one prompt version spanning two fingerprints). Carried so a cell holding more than
    # one says so rather than pooling silently.
    prompt_versions: Tuple[str, ...] = ()
    thin: bool = False                       # below the configured `min_passes`

    @property
    def weekday_label(self) -> str:
        return _WEEKDAYS[self.weekday - 1] if 1 <= self.weekday <= 7 else '?'

    @property
    def candidates_avg(self) -> float:
        """Mean `in_window`. Capped at `top_k * _OVERFETCH` by the retriever, so a value AT the cap
        is a full candidate pool and a value below it is a genuinely thin window — which is what
        separates 'the floor cut more' from 'there was less to cut'."""
        return self.candidates_sum / self.symbol_passes if self.symbol_passes else 0.0

    @property
    def cut_pct(self) -> float:
        """Share of the candidate pool the relevance floor removed."""
        return 100.0 * self.floor_dropped_sum / self.candidates_sum if self.candidates_sum else 0.0

    @property
    def kept_avg(self) -> float:
        return self.kept_sum / self.symbol_passes if self.symbol_passes else 0.0

    @property
    def deep_kept_avg(self) -> float:
        return self.deep_kept_sum / self.symbol_passes if self.symbol_passes else 0.0

    @property
    def best_distance_avg(self) -> Optional[float]:
        if not self.best_distance_count:
            return None
        return self.best_distance_sum / self.best_distance_count

    @property
    def floor_conflict(self) -> bool:
        """The floor was retuned inside this cell, so its averages straddle two configurations."""
        return len(self.floors) > 1

    @property
    def version_conflict(self) -> bool:
        return len(self.prompt_versions) > 1


@dataclass
class RetrievalDriftDelta:
    """The step between two consecutive fingerprints on the SAME pipeline and weekday.

    Computed here rather than in the renderer so the console and the HTTP payload cannot disagree
    about the comparison — and because the delta, not the absolute rows, is what the report is for:
    the raw numbers are precisely what a reader mis-compares across weekdays.
    """
    pipeline_id: str
    weekday: int
    from_fingerprint: str
    to_fingerprint: str
    cut_pct_delta: float                     # percentage POINTS, not a ratio
    kept_delta: float
    candidates_delta: float
    best_distance_delta: Optional[float]     # None when either side scored nothing
    thin: bool                               # either side below `min_passes`
    candidates_before: float = 0.0           # the pool `candidates_delta` is measured against
    deep_changed: bool = False               # the deep tier came on or went off across the step

    @property
    def weekday_label(self) -> str:
        return _WEEKDAYS[self.weekday - 1] if 1 <= self.weekday <= 7 else '?'

    @property
    def basis_changed(self) -> bool:
        """The candidate pool is not the same kind of thing on both sides.

        `cut_pct` and `best_distance` are measured over the nearest-N pool, so they compare only
        while that pool means the same thing. Switching the deep tier on adds a SECOND query
        reaching a week back: the pool doubles with deliberately older, further candidates, most of
        which the floor removes. Both columns then move for a structural reason and neither says
        anything about the corpus.

        Measured on production 2026-09-01, `crypto_sentiment` Tuesday: the pool went 24.0 -> 48.0
        and `cut%` 11.7 -> 46.1 with `kept` at 11.5 -> 11.3 — the prompt was unchanged and only the
        basis had moved. Ordinary weekday variation in the same run runs 0 % (a full pool on both
        sides) to 26 % (a thin Sunday window), so half the pool is a wide margin between the two.
        """
        if self.deep_changed:
            return True
        if not self.candidates_before:
            return False
        return abs(self.candidates_delta) / self.candidates_before >= _POOL_RESHAPED

    @property
    def reading(self) -> str:
        """The one sentence the pair supports — deliberately narrow.

        Only the combination of the two columns is diagnostic, so this never names a cause from
        `cut_pct` alone: a floor meeting a wider spread and a corpus drifting away from the query
        move the same column in the same direction.

        **The distance's SIGN is what selects the sentence, not its magnitude.** The first version
        tested `abs(best_distance_delta)`, which folded "the cut rose while the nearest got closer"
        into the branch for "the nearest got further" and reported the corpus as moving away when it
        had moved toward. It mislabelled the one comparison this report was built for — production
        `forex_macro_sentiment`, Tuesday, cut +10.4pp with `best` improving 0.411 → 0.395.

        That mixed case is not an edge: it is the sharpening signature. Good matches get better
        while marginal ones fall outside a fixed floor, which is what a text or embedding change
        does to a distance distribution — and it is a different finding from either pure direction.
        """
        if self.basis_changed:
            # Refusing to interpret IS the finding here. Both rate columns moved because the pool
            # they are rates over changed shape, and naming a cause from them would be the same
            # mistake as reading a deploy across a weekend — a number carried over a boundary where
            # its basis moved. `kept` and `deep` are measured per pass and still compare.
            return ('candidate pool changed shape (deep tier on/off) — cut% and distance are not '
                    'comparable across this step; read kept and deep')
        if self.best_distance_delta is None:
            return 'no distance on one side — not comparable'
        if abs(self.cut_pct_delta) < _CUT_FLAT:
            return 'floor cut steady'
        if self.best_distance_delta >= _DISTANCE_FLAT:
            nearest = 'further'
        elif self.best_distance_delta <= -_DISTANCE_FLAT:
            nearest = 'closer'
        else:
            nearest = 'flat'
        if self.cut_pct_delta > 0:
            return {
                'further': 'cut rose WITH the distance — the corpus moved away from the query',
                'flat': 'cut rose while the nearest stayed put — the floor met a wider spread',
                'closer': 'cut rose while the nearest got CLOSER — the distribution sharpened: '
                          'better best matches, more of the pool outside a fixed floor',
            }[nearest]
        return {
            'closer': 'cut fell WITH the distance — the corpus moved toward the query',
            'flat': 'cut fell while the nearest stayed put — a narrower spread',
            'further': 'cut fell while the nearest got FURTHER — the pool tightened around a '
                       'weaker best match',
        }[nearest]


@dataclass
class RetrievalDriftReport:
    since_label: str
    min_passes: int
    rows: List[RetrievalDriftRow] = field(default_factory=list)
    deltas: List[RetrievalDriftDelta] = field(default_factory=list)
    fingerprints_seen: int = 0
    envelopes: int = 0

    @property
    def thin_rows(self) -> int:
        return sum(1 for row in self.rows if row.thin)

    @property
    def comparable_weekdays(self) -> int:
        """Weekdays that actually carry a before/after pair — the report's usable evidence."""
        return len({(delta.pipeline_id, delta.weekday) for delta in self.deltas})


def build_retrieval_drift_report(database_url: str, since: datetime, *,
                                 since_label: str = '14d', min_passes: int = 40,
                                 outcomes_table: str = 'outcomes') -> RetrievalDriftReport:
    """Read the persisted funnels in the window and fold them into per-cell accumulators."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # No outcomes table yet = nothing produced; a clean empty report, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (outcomes_table,))
            if cur.fetchone()[0] == 0:
                return RetrievalDriftReport(since_label, min_passes)
            cur.execute(
                f'SELECT pipeline_id, ts, envelope FROM {outcomes_table} '
                "WHERE ts >= %s AND status <> 'error' ORDER BY pipeline_id, ts",
                (since,))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise VectorStoreError(f'retrieval-drift report failed: {exc}') from exc

    return aggregate_retrieval_drift(rows, since_label, min_passes)


def aggregate_retrieval_drift(rows: List[Tuple[str, datetime, Any]], since_label: str,
                              min_passes: int) -> RetrievalDriftReport:
    """Fold envelopes into per-cell accumulators — the DB-free core, and the tested one."""
    cells: Dict[Tuple[str, str, int], RetrievalDriftRow] = {}
    envelopes = 0

    for pipeline_id, ts, envelope in rows:
        env = envelope if isinstance(envelope, dict) else json.loads(envelope)
        funnels = (env.get('metadata') or {}).get('per_symbol_retrieval') or {}
        if not funnels:
            continue                          # pre-ISSUE_24 envelope: nothing to attribute
        envelopes += 1
        # An envelope produced before ISSUE_85 carries no fingerprint. It is still a real pass, so
        # it is grouped under an explicit marker rather than dropped or folded into a neighbour.
        fingerprint = env.get('config_fingerprint') or '(unstamped)'
        key = (pipeline_id, fingerprint, ts.isoweekday())
        cell = cells.get(key)
        if cell is None:
            cell = RetrievalDriftRow(
                pipeline_id=pipeline_id, config_fingerprint=fingerprint,
                weekday=ts.isoweekday(), symbol_passes=0, first_seen=ts, last_seen=ts,
                candidates_sum=0, floor_dropped_sum=0, kept_sum=0, deep_kept_sum=0,
                best_distance_sum=0.0, best_distance_count=0)
            cells[key] = cell
        cell.last_seen = ts
        floors = set(cell.floors)
        versions = set(cell.prompt_versions)
        version = env.get('prompt_version')
        if version is not None:
            versions.add(str(version))
        for funnel in funnels.values():
            if not isinstance(funnel, dict):
                continue
            cell.symbol_passes += 1
            cell.candidates_sum += int(funnel.get('in_window') or 0)
            cell.floor_dropped_sum += int(funnel.get('floor_dropped') or 0)
            cell.kept_sum += int(funnel.get('kept') or 0)
            # Absent on envelopes produced before the deep tier existed — 0 is the correct reading:
            # the tier was off, so it carried nothing.
            cell.deep_kept_sum += int(funnel.get('deep_kept') or 0)
            best = funnel.get('best_distance')
            if best is not None:
                cell.best_distance_sum += float(best)
                cell.best_distance_count += 1
            if funnel.get('floor') is not None:
                floors.add(float(funnel['floor']))
        cell.floors = tuple(sorted(floors))
        cell.prompt_versions = tuple(sorted(versions))

    # Within one pipeline and weekday, order by when the fingerprint first appeared: the delta then
    # reads "later minus earlier", which is the direction a before/after has to have.
    ordered = sorted(cells.values(),
                     key=lambda row: (row.pipeline_id, row.weekday,
                                      row.first_seen or datetime.min, row.config_fingerprint))
    for row in ordered:
        row.thin = row.symbol_passes < min_passes

    report = RetrievalDriftReport(
        since_label=since_label, min_passes=min_passes, rows=ordered,
        deltas=_deltas(ordered),
        fingerprints_seen=len({row.config_fingerprint for row in ordered}),
        envelopes=envelopes)
    return report


def _deltas(ordered: List[RetrievalDriftRow]) -> List[RetrievalDriftDelta]:
    """Consecutive pairs inside one (pipeline, weekday) group.

    Consecutive rather than first-against-last: a weekday can hold more than two fingerprints
    (2026-08-25 carried five for `crypto_sentiment` in one day), and a step between neighbours stays
    meaningful there while a first/last span would silently average over everything between.
    """
    deltas: List[RetrievalDriftDelta] = []
    for index in range(1, len(ordered)):
        before, after = ordered[index - 1], ordered[index]
        if (before.pipeline_id, before.weekday) != (after.pipeline_id, after.weekday):
            continue
        distance_delta = None
        if before.best_distance_avg is not None and after.best_distance_avg is not None:
            distance_delta = after.best_distance_avg - before.best_distance_avg
        deltas.append(RetrievalDriftDelta(
            pipeline_id=after.pipeline_id, weekday=after.weekday,
            from_fingerprint=before.config_fingerprint,
            to_fingerprint=after.config_fingerprint,
            cut_pct_delta=after.cut_pct - before.cut_pct,
            kept_delta=after.kept_avg - before.kept_avg,
            candidates_delta=after.candidates_avg - before.candidates_avg,
            best_distance_delta=distance_delta,
            thin=before.thin or after.thin,
            candidates_before=before.candidates_avg,
            # The deep tier appearing or disappearing is a basis change even when it kept nothing,
            # so it is detected from the tier's presence rather than only from the pool's size.
            deep_changed=(before.deep_kept_sum > 0) != (after.deep_kept_sum > 0)))
    return deltas


def _distance(value: Optional[float]) -> str:
    return f'{value:.3f}' if value is not None else '    —'


def format_retrieval_drift_report(report: RetrievalDriftReport) -> str:
    """The shared console pattern: title, window line, `----` dividers, aligned columns."""
    lines = ['Retrieval Drift — did the evidence move when the setup changed',
             f'window: last {report.since_label} · {report.envelopes} envelopes · '
             f'{report.fingerprints_seen} config fingerprints · '
             f'{report.comparable_weekdays} weekdays with a before/after pair',
             'rows are per WEEKDAY on purpose: a deploy usually changes the day of the week too, '
             'and reading across that is how a weekend gets attributed to a release',
             '-' * 102]
    if not report.rows:
        lines.append('no envelope in the window carries a retrieval funnel — nothing to compare')
        return '\n'.join(lines)

    lines.append(f'{"pipeline":24s} {"dow":>4s} {"fingerprint":14s} {"passes":>7s} '
                 f'{"cand":>6s} {"cut%":>6s} {"best":>6s} {"kept":>6s} {"deep":>6s} {"floor":>12s}')
    lines.append('-' * 102)

    deltas_by_position = {(delta.pipeline_id, delta.weekday, delta.to_fingerprint): delta
                          for delta in report.deltas}
    for row in report.rows:
        floor = '/'.join(f'{value:.2f}' for value in row.floors) or '—'
        marks = ''.join(('  ⚠ thin' if row.thin else '',
                         '  ⚠ floor retuned' if row.floor_conflict else '',
                         '  ⚠ mixed prompt version' if row.version_conflict else ''))
        lines.append(
            f'{row.pipeline_id:24s} {row.weekday_label:>4s} {row.config_fingerprint:14s} '
            f'{row.symbol_passes:7d} {row.candidates_avg:6.1f} {row.cut_pct:6.1f} '
            f'{_distance(row.best_distance_avg):>6s} {row.kept_avg:6.1f} '
            f'{row.deep_kept_avg:6.2f} {floor:>12s}{marks}')
        delta = deltas_by_position.get((row.pipeline_id, row.weekday, row.config_fingerprint))
        if delta is not None:
            distance = (f'{delta.best_distance_delta:+.3f}'
                        if delta.best_distance_delta is not None else '—')
            lines.append(
                f'{"":>29s}→ same weekday vs {delta.from_fingerprint}: '
                f'cut {delta.cut_pct_delta:+.1f}pp · kept {delta.kept_delta:+.1f} · '
                f'best {distance}'
                f'{"  (thin)" if delta.thin else ""}')
            lines.append(f'{"":>29s}  {delta.reading}')

    lines.append('-' * 102)
    if report.thin_rows:
        lines.append(f'{report.thin_rows} of {len(report.rows)} cells hold fewer than '
                     f'{report.min_passes} symbol-passes — marked thin, not dropped: a small cell is '
                     'still evidence about which fingerprint ran that day')
    if not report.deltas:
        lines.append('no weekday carries two fingerprints yet — nothing to compare within a day. '
                     'A window of at least two weeks is what makes matched weekdays possible')
    lines.append('cand = mean candidate pool (capped by the retriever, so a value below the cap '
                 'means a thin window, not a stricter floor — and a value ABOVE it means a second '
                 'tier is running)')
    # Four readings, because there are four: the two mixed cases are not edges. The first shipped
    # version of this legend named two, while the body already printed four — and the mixed case it
    # omitted turned out to be the most informative row in the production run.
    lines.append('the pair, not either column alone — cut% rising beside a best distance that is')
    lines.append('    FLAT    → the floor is meeting a wider spread')
    lines.append('    RISING  → the corpus moved away from the query')
    lines.append('    FALLING → the distribution sharpened: better best matches, more of the pool '
                 'outside a fixed floor')
    lines.append('and a falling cut% beside a RISING distance is the pool tightening around a '
                 'weaker best match')
    lines.append('when the pool itself changes shape (a tier switched on) both rate columns move '
                 'for a structural reason and neither is read — kept and deep still compare')
    return '\n'.join(lines)
