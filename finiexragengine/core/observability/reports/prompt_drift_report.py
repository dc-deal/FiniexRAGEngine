"""Prompt drift — the urgency distribution per prompt version (ISSUE_110).

A prompt change is a series break by construction, and until now nothing compared the two sides of
one. `prompt_version` and `prompt_hash` ride on every envelope exactly as ISSUE_33 built them — the
provenance control worked perfectly and is the wrong control, because a label is not a comparison.
v2 → v3 (2026-08-23) cut the crypto confirm rate 8.43 % → 0.47 %, 113 breaking rows a day down to
six, and it took three days to see.

Three properties are therefore designed in rather than left to the reader:

- **Never pooled.** Grouping is per pipeline, always. The v3 → v4 measurement is the argument: the
  aggregate across both streams moved 6.67 % → 6.60 %, practically unchanged, while both
  distributions underneath were rebuilt. No field here holds a cross-pipeline figure and no line of
  the rendering prints one — a one-number report cannot make the true statement.
- **The confirm band carries its concentration.** Forex v3 reads healthy at 10.78 % and collapsed at
  "one analysis unit supplies 93 % of it". The share alone is the near-miss this report exists to
  prevent, so it never travels without the unit count beside it.
- **Only LLM-scored passes enter the distribution.** A result with `basis != 'llm'` is a mechanical
  `no_data` HOLD: retrieval came back empty after the floor, no LLM call was made, and the row
  carries `urgency 0.0`. Folding those in means a corpus outage — the 37-hour frozen corpus of
  2026-08-20 — reads as "the new prompt got calmer". `mechanical` is reported beside `scored` rather
  than dropped, because an absent number is not an answer either.

Read from the store like every other surface here, so it re-derives the whole history under whatever
hold gate is configured today rather than only describing the future.
"""
import json
import math
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import psycopg

from finiexragengine.core.observability.reports.breaking_report import PipelineGroupings
from finiexragengine.core.pipeline.breaking_episode_rule import (
    BreakingEpisodeRule,
    EpisodeGrouping,
)
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# Above this many distinct urgency values in one version, the model has stopped quantising and a
# per-value column set becomes forty columns wide. The report then bins to 0.1 and says so — the
# seven-value lattice (ISSUE_82) is a measured property of a prompt, never a contract.
_MAX_DISTINCT = 12
_BIN_WIDTH = 0.1


def _fmt_bucket(value: float) -> str:
    """A bucket's key and column label — two decimals, so keys sort as they render."""
    return f'{value:.2f}'


@dataclass
class VersionDistribution:
    """One (pipeline, prompt version) pair: how its scores were distributed, and how concentrated.

    The shares are properties rather than stored fields so the console and the API payload cannot
    disagree — `utils/dataclass_json.to_jsonable` carries public properties, so the derivation
    exists once.
    """
    pipeline_id: str
    prompt_version: str
    prompt_id: str = ''
    prompt_hashes: List[str] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    scored: int = 0                  # LLM-scored analysis-unit passes — the evidence
    mechanical: int = 0              # `basis != 'llm'`: no LLM call was made
    histogram: Dict[str, int] = field(default_factory=dict)      # bucket label -> scored passes
    confirm_passes: int = 0          # the recorded `is_breaking`, never re-derived
    hold_passes: int = 0             # below the confirm gate, at or above the hold gate
    # Per analysis unit, how many of its passes confirmed. Published rather than reduced to the two
    # numbers below: the concentration claim has to be checkable against the rows that make it.
    # Keyed by the EPISODE KEY, which for FX is the retrieval query ('US Dollar Canadian Dollar
    # USD/CAD Bank of Canada BOC') — traceable, and the same key `breaking_episode_id` is built
    # from. `unit_labels` carries the tickers for rendering, exactly as `SymbolTimeline.label()`
    # does: counting by query and displaying by query are two different requirements, and only the
    # first one is served by the key.
    unit_confirms: Dict[str, int] = field(default_factory=dict)
    unit_labels: Dict[str, str] = field(default_factory=dict)

    @property
    def confirm_share(self) -> float:
        return self.confirm_passes / self.scored if self.scored else 0.0

    @property
    def hold_share(self) -> float:
        return self.hold_passes / self.scored if self.scored else 0.0

    @property
    def hold_break_ratio(self) -> Optional[float]:
        """Hold-band passes per confirming pass — `None` when nothing confirmed.

        The number that makes v3's collapse legible: its confirm share fell 18-fold while it kept
        parking one step below the gate, so the ratio went 2.3 → 19.1. A bare confirm share cannot
        distinguish "the model stopped seeing urgency" from "the model stopped crossing the line".
        """
        return self.hold_passes / self.confirm_passes if self.confirm_passes else None

    @property
    def confirm_units(self) -> int:
        return len(self.unit_confirms)

    @property
    def top_unit(self) -> str:
        if not self.unit_confirms:
            return ''
        return max(self.unit_confirms.items(), key=lambda item: (item[1], item[0]))[0]

    @property
    def top_unit_share(self) -> float:
        """The largest single unit's share OF THE CONFIRM BAND — not of all passes."""
        if not self.confirm_passes:
            return 0.0
        return self.unit_confirms.get(self.top_unit, 0) / self.confirm_passes

    @property
    def top_unit_label(self) -> str:
        """The tickers behind `top_unit` — what a reader recognises, not the retrieval query."""
        return self.unit_labels.get(self.top_unit, self.top_unit)

    @property
    def hash_conflict(self) -> bool:
        """More than one prompt body under one version — an in-place edit the rules forbid."""
        return len(self.prompt_hashes) > 1

    @property
    def model_conflict(self) -> bool:
        """More than one eval model under one version: the distribution has two causes, not one."""
        return len(self.models) > 1

    @property
    def single_unit_confirm_band(self) -> bool:
        """The forex-v3 shape: a share that reads healthy while one unit supplies all of it."""
        return self.confirm_passes > 0 and self.confirm_units == 1


@dataclass
class PipelineDistribution:
    """One pipeline's versions, and the two gates its rows are read against."""
    pipeline_id: str
    # Today's configured confirm gate, shown for context only — the confirm counts come from the
    # recorded verdict, so a retune since then cannot rewrite what the archive says happened.
    confirm_threshold: Optional[float] = None
    exit_threshold: float = 0.7          # applied at read time, exactly as the episode rule does
    versions: List[VersionDistribution] = field(default_factory=list)


@dataclass
class PromptDriftReport:
    since_label: str
    pipelines: List[PipelineDistribution] = field(default_factory=list)
    buckets: List[float] = field(default_factory=list)   # the observed value set, descending
    binned: bool = False                                 # true = folded to 0.1, see `_MAX_DISTINCT`
    since: Optional[datetime] = None
    until: Optional[datetime] = None

    @property
    def version_count(self) -> int:
        return sum(len(pipeline.versions) for pipeline in self.pipelines)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


# Read a PROJECTION of each envelope, never the whole thing. The report consumes nine leaves; a
# served envelope is ~42 KB, and `reasoning` plus `sources` are almost all of it. Measured against
# production 2026-08-26: a 7-day window is ~2,200 envelopes and a 30-day one — the configured
# default — is ~9,400, so reading them whole is a transient of order a gigabyte once parsed, on a
# 16 GB host. The window it was sized against in development held 1,400.
#
# Two details are load-bearing rather than stylistic:
#   * `WITH ORDINALITY` + `ORDER BY ord` keeps the result array in its stored order. The
#     aggregation itself is order-free (max urgency, any verdict), but the ticker LABEL of a fanned
#     pair is built by first appearance — without the ordering `ETHUSD/ETHEUR` could render as
#     `ETHEUR/ETHUSD` between two runs of the same query.
#   * `COALESCE(..., '[]')` twice: an envelope with no `result` key, and a result array that
#     aggregates to nothing, must both arrive as an empty list rather than as SQL NULL.
#
# What it must NOT become is a second aggregation. Everything below `_aggregate_drift` still sees a
# plain envelope-shaped dict, so the DB-free core stays the single place where a number is decided
# and the suite keeps driving it with whole envelopes.
_PROJECTION = """
    SELECT pipeline_id, jsonb_build_object(
        'timestamp',      envelope -> 'timestamp',
        'prompt_version', envelope -> 'prompt_version',
        'prompt_id',      envelope -> 'prompt_id',
        'prompt_hash',    envelope -> 'prompt_hash',
        'metadata',       jsonb_build_object('model', envelope -> 'metadata' -> 'model'),
        'result',         COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'symbol',        row -> 'symbol',
                       'base_currency', row -> 'base_currency',
                       'basis',         row -> 'basis',
                       'urgency',       row -> 'urgency',
                       'is_breaking',   row -> 'is_breaking')
                   ORDER BY ord)
            FROM jsonb_array_elements(COALESCE(envelope -> 'result', '[]'::jsonb))
                 WITH ORDINALITY AS elements(row, ord)), '[]'::jsonb)
    ) AS envelope
    FROM {table}
    WHERE ts >= %s AND status <> 'error'
    ORDER BY pipeline_id, ts
"""


def build_prompt_drift_report(database_url: str, since: datetime, *,
                             since_label: str = '30d',
                             outcomes_table: str = 'outcomes',
                             rules: Optional[PipelineGroupings] = None,
                             confirm_thresholds: Optional[Dict[str, float]] = None,
                             ) -> PromptDriftReport:
    """The per-version score distribution over the window, per pipeline.

    `rules` carries each pipeline's hold gate and episode key, resolved by the caller from the
    registry factories (the only load path that honours the `user_configs/` overlay). The key
    matters here for the same reason it matters to the episode count: a fanned pair (ISSUE_70) is
    one analysis, and counting both legs would inflate exactly the concentration figure that is
    supposed to expose a single-symbol confirm band.
    """
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # No outcomes table yet = nothing produced; a clean empty report, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (outcomes_table,))
            if cur.fetchone()[0] == 0:
                return PromptDriftReport(since_label)
            cur.execute(_PROJECTION.format(table=outcomes_table), (since,))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise VectorStoreError(f'prompt drift report failed: {exc}') from exc
    return _aggregate_drift(rows, since_label, rules or {}, confirm_thresholds or {},
                            since=since, until=datetime.now(timezone.utc))


def _unit_samples(env: Dict[str, object], grouping: EpisodeGrouping) -> Tuple[
        Dict[str, float], Dict[str, bool], List[str], Dict[str, str]]:
    """Collapse one envelope's results to one sample per analysis unit.

    Returns `(urgency per scored unit, recorded verdict per scored unit, mechanical units, the
    ticker label per unit)`. The
    collapse is `max` on urgency and `any` on the verdict — the same "strongest state wins" rule the
    timeline report applies to a bucket, for the same reason: one confirming leg is the thing you
    are looking for, and averaging it away would defeat the report.
    """
    urgencies: Dict[str, float] = {}
    verdicts: Dict[str, bool] = {}
    labels: Dict[str, List[str]] = {}
    mechanical: List[str] = []
    for result in env.get('result', []) or []:
        symbol = str(result.get('symbol') or '')
        unit = grouping.key_for(symbol, result.get('base_currency'))
        if symbol and symbol not in labels.setdefault(unit, []):
            labels[unit].append(symbol)
        if result.get('basis') not in (None, 'llm'):
            # Not evidence about the prompt: retrieval was empty, so the model never saw this pass.
            if unit not in urgencies:
                mechanical.append(unit)
            continue
        urgency = float(result.get('urgency') or 0.0)
        urgencies[unit] = max(urgencies.get(unit, 0.0), urgency)
        verdicts[unit] = verdicts.get(unit, False) or bool(result.get('is_breaking'))
    # A unit with one scored leg is a scored unit, whatever its other legs did.
    return (urgencies, verdicts,
            [unit for unit in dict.fromkeys(mechanical) if unit not in urgencies],
            {unit: '/'.join(symbols) for unit, symbols in labels.items()})


def _aggregate_drift(rows: List[Tuple[str, object]], since_label: str,
                     rules: PipelineGroupings, confirm_thresholds: Dict[str, float], *,
                     since: Optional[datetime] = None,
                     until: Optional[datetime] = None) -> PromptDriftReport:
    """Build the per-version distributions — the DB-free core (tested)."""
    groupings: Dict[str, EpisodeGrouping] = {}
    built: Dict[Tuple[str, str], VersionDistribution] = {}
    # Raw value counters, kept apart from the rendered histogram: whether to bin can only be decided
    # once every version has been seen.
    samples: Dict[Tuple[str, str], Counter] = {}

    for pipeline_id, envelope in rows:
        env = envelope if isinstance(envelope, dict) else json.loads(envelope)
        grouping = groupings.setdefault(
            pipeline_id, rules.get(pipeline_id) or EpisodeGrouping(BreakingEpisodeRule()))
        # A legacy envelope with no prompt version is its own row rather than a dropped one: the
        # archive reaches back before the field existed, and silence about it would be a gap.
        version = str(env.get('prompt_version') or '(none)')
        key = (pipeline_id, version)
        row = built.setdefault(key, VersionDistribution(pipeline_id, version))
        counter = samples.setdefault(key, Counter())

        row.prompt_id = row.prompt_id or str(env.get('prompt_id') or '')
        prompt_hash = str(env.get('prompt_hash') or '')
        if prompt_hash and prompt_hash not in row.prompt_hashes:
            row.prompt_hashes.append(prompt_hash)
        model = str((env.get('metadata') or {}).get('model') or '')
        if model and model not in row.models:
            row.models.append(model)

        ts = _parse_dt(str(env['timestamp']))
        row.first_seen = ts if row.first_seen is None else min(row.first_seen, ts)
        row.last_seen = ts if row.last_seen is None else max(row.last_seen, ts)

        exit_threshold = grouping.rule.get_exit_threshold()
        urgencies, verdicts, mechanical, labels = _unit_samples(env, grouping)
        row.unit_labels.update(labels)
        row.mechanical += len(mechanical)
        for unit, urgency in urgencies.items():
            row.scored += 1
            counter[urgency] += 1
            if verdicts.get(unit):
                row.confirm_passes += 1
                row.unit_confirms[unit] = row.unit_confirms.get(unit, 0) + 1
            elif urgency >= exit_threshold:
                # The hold band, defined exactly as the timeline report's `.` cell defines it, so
                # one word means one thing across both surfaces.
                row.hold_passes += 1

    # Bin only if a single version needs it — one runaway version must not re-key the others into a
    # coarser grid than they earned, but the columns have to be one shared set to compare across.
    binned = any(len(counter) > _MAX_DISTINCT for counter in samples.values())
    for key, counter in samples.items():
        folded: Counter = Counter()
        for value, count in counter.items():
            folded[_bin(value) if binned else value] += count
        built[key].histogram = {_fmt_bucket(value): folded[value]
                                for value in sorted(folded, reverse=True)}

    buckets = sorted({value for counter in samples.values()
                      for value in (_bin(v) if binned else v for v in counter)}, reverse=True)
    pipelines: List[PipelineDistribution] = []
    for pipeline_id in sorted({key[0] for key in built}):
        grouping = groupings[pipeline_id]
        versions = [row for key, row in built.items() if key[0] == pipeline_id]
        # Chronological by first appearance: the drift is a sequence, and reading it in time order
        # is the whole point of putting the versions on adjacent lines.
        versions.sort(key=lambda row: (row.first_seen or datetime.max.replace(tzinfo=timezone.utc),
                                       row.prompt_version))
        pipelines.append(PipelineDistribution(
            pipeline_id, confirm_threshold=confirm_thresholds.get(pipeline_id),
            exit_threshold=grouping.rule.get_exit_threshold(), versions=versions))
    return PromptDriftReport(since_label, pipelines, buckets, binned, since=since, until=until)


def _bin(value: float) -> float:
    """Fold a value onto the 0.1 grid below it — the fallback when quantisation is gone."""
    return round(math.floor(round(value / _BIN_WIDTH, 6)) * _BIN_WIDTH, 2)


def _pct(share: float) -> str:
    return f'{share * 100:.2f}%'


def _ratio(value: Optional[float]) -> str:
    return '—' if value is None else f'{value:.1f}'


def format_prompt_drift_report(report: PromptDriftReport, *,
                               width: Optional[int] = None) -> str:
    """Render as the shared console pattern: title, window line, `----` dividers, aligned columns.

    One block per pipeline, one line per version, and **no total line across pipelines** — the
    pooled figure is the mistake this report was built after, so the rendering has nowhere to put
    one.
    """
    term_width = width if width is not None else shutil.get_terminal_size((120, 24)).columns
    labels = [_fmt_bucket(value) for value in report.buckets]
    head = f'{"version":<8} {"first seen":<14} {"scored":>7} {"mech":>5}'
    head += ''.join(f'{label:>7}' for label in labels)
    head += f'{"confirm":>9}{"hold":>8}{"h/b":>6}{"units":>6}  top unit'

    lines: List[str] = ['Prompt drift — urgency distribution per prompt version']
    window = f'window: last {report.since_label}'
    if report.since and report.until:
        window += (f'   {report.since.strftime("%m-%d %H:%M")} → '
                   f'{report.until.strftime("%m-%d %H:%M")} UTC')
    lines.append(window)
    if report.binned:
        lines.append(f'urgency binned to {_BIN_WIDTH} — a version emitted more than '
                     f'{_MAX_DISTINCT} distinct values, so the lattice no longer holds')

    if not report.pipelines:
        lines.append('-' * min(term_width, len(head)))
        lines.append('(no passes in the window)')
        return '\n'.join(lines)

    flags: List[str] = []
    for pipeline in report.pipelines:
        divider = '-' * min(term_width, len(head))
        gate = ('confirm gate not configured' if pipeline.confirm_threshold is None
                else f'confirm gate {pipeline.confirm_threshold:.2f} (config today)')
        models = sorted({model for version in pipeline.versions for model in version.models})
        # Named only when there is one. A header reading "gpt-4o, gpt-4o-mini" would claim the
        # pipeline runs two models when one version used a second for a single pass — a summary
        # line contradicting its own rows, which is the flattening this whole report was built
        # against. More than one defers to the per-row flags.
        model_line = (f'model {models[0]}' if len(models) == 1
                      else f'{len(models)} models (see flags)' if models else 'model —')
        lines.extend([
            '',
            f'{pipeline.pipeline_id} · {gate} · hold band {pipeline.exit_threshold:.2f} (applied) '
            f'· {model_line}',
            divider,
            head,
            divider,
        ])
        for version in pipeline.versions:
            seen = version.first_seen.strftime('%m-%d %H:%M') if version.first_seen else '—'
            cells = ''.join(
                f'{_pct(version.histogram.get(label, 0) / version.scored) if version.scored else "—":>7}'
                for label in labels)
            mark = ' ⚠' if (version.single_unit_confirm_band or version.hash_conflict
                            or version.model_conflict) else ''
            top = (f'{version.top_unit_label} {version.top_unit_share * 100:.0f}%'
                   if version.top_unit else '—')
            lines.append(
                f'v{version.prompt_version:<7} {seen:<14} {version.scored:>7} '
                f'{version.mechanical:>5}{cells}{_pct(version.confirm_share):>9}'
                f'{_pct(version.hold_share):>8}{_ratio(version.hold_break_ratio):>6}'
                f'{version.confirm_units:>6}  {top}{mark}')
            # The flags say what the ⚠ meant. Only for states actually present, so the legend never
            # explains a condition the report did not find.
            name = f'{pipeline.pipeline_id} v{version.prompt_version}'
            if version.single_unit_confirm_band:
                lines_flag = (f'⚠ {name}: the confirm band rests on a single analysis unit '
                              f'({version.top_unit_label}) — the share alone reads healthy')
                flags.append(lines_flag)
            if version.hash_conflict:
                flags.append(f'⚠ {name}: {len(version.prompt_hashes)} prompt hashes under one '
                             f'version ({", ".join(version.prompt_hashes)}) — edited in place')
            if version.model_conflict:
                flags.append(f'⚠ {name}: {len(version.models)} eval models '
                             f'({", ".join(version.models)}) — two causes, not one')
        lines.append(divider)

    lines.extend(flags)
    lines.append(f'{len(report.pipelines)} pipeline(s) · {report.version_count} version(s) · '
                 f'shares are over LLM-scored passes; no pooled figure is emitted')
    return '\n'.join(lines)
