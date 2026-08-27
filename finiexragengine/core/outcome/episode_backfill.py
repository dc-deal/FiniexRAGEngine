"""Backfill episode identity into the archived series (ISSUE_108) — one replay, two sinks.

ISSUE_65 populated the assembly path only and said so, which leaves the consumer's archived range
(2026-07-16 → 08-20) carrying neither episode field. They key their strategy on
`breaking_episode_id` and cannot backtest an episode-gated strategy against an archive that does not
have it.

**It drives `BreakingEpisodeTracker`, not a walk lifted out of the funnel report** — a deliberate
deviation from the issue's own DoD, for a reason the code settles: `breaking_report._aggregate` never
calls `episode_id()`, so the report cannot produce identities at all. Only the tracker mints them,
and the hard part is not the rule but the bookkeeping around it — the per-key id map, the
adopt-an-existing-id branch, and the release that lets the next episode on a key mint fresh. Driving
the tracker gives parity with the **live path** rather than with a second read-time derivation: the
same function that minted every served id mints the backfilled ones, and the `EpisodeUpsert` rows and
reaction times fall out of it. `BreakingEpisodeTracker.seed` already replays persisted envelopes
through `observe` for exactly this reason — *"seeding cannot drift from scoring"*.

**It is a reconstruction, not a recovery.** Three read-time policy changes landed inside the
consumer's window: hysteresis (2026-08-17), `EPISODE_GAP` 45 → 150 (08-18), and the episode key
moving from base currency to the retrieval query (08-18). The last is consequential — before 08-18
`USDJPY`/`USDCAD`/`USDCHF` shared one `USD` key, so one symbol's story held another's episode open.
A replay under today's grouping splits them correctly, which makes the backfilled FX episodes for
that stretch **better than what was served, and different from it**. Every surface says so.

**The range needs a prologue, and that is not in the issue.** An episode that opened before `since`
is still open inside it; replaying from `since` cold would mint an id from a *clipped* start — a
second identity for a story already running, which is the failure the tracker's adopt-branch exists
to prevent at boot. So a window before `since` is replayed for state only and never written. The
caller resolves its width the way `pipeline_assembler` does, `max(2 × gap, episode_seed_hours)`,
because that number is calibrated against measured hold-band tails (5 h, 8.7 h, 33 h) rather than
guessed.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg

from finiexragengine.core.outcome.episode_registry import EpisodeRegistry
from finiexragengine.core.pipeline.breaking_episode import BreakingEpisodeTracker
from finiexragengine.core.pipeline.breaking_episode_rule import (
    BreakingEpisodeRule,
    EpisodeGrouping,
)
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.eval_types import EpisodeUpsert
from finiexragengine.types.outcome_types import SentimentEnvelope

logger = logging.getLogger(__name__)


@dataclass
class Disagreement:
    """A row whose stored identity differs from what the replay computes for it.

    The self-check ISSUE_108 asks for, and the reason to replay *past* the ISSUE_65 deploy even
    though nothing past it needs writing: where the archive already carries an id, the replay must
    reproduce it, and that comparison is the backfill testing itself on real data before it writes.

    Not every disagreement is a defect. For an episode open across the ISSUE_65 deploy the served id
    was minted by a process whose own seed window clipped the start, so a different `started_at` —
    and therefore a different id — is possible with no rule divergence at all. Because the write
    rule is "only where absent", such a case can never corrupt anything; it is a diagnostic for a
    human. Hence both values travel together and the decision stays outside this unit.
    """
    pipeline_id: str
    row_id: int
    ts: datetime
    symbol: str
    served_id: str
    computed_id: str
    served_start: bool
    computed_start: bool


@dataclass
class PipelineBackfill:
    """What the replay found for one pipeline inside the range."""
    pipeline_id: str
    envelopes: int = 0            # in-range envelopes replayed
    results: int = 0              # analysis results inside them
    would_stamp: int = 0          # results with no identity that the replay gives one
    carried: int = 0              # results that already had one — the self-check population
    openers: int = 0              # of the stamped ones, those opening an episode
    episodes: int = 0             # distinct episode ids touched in range
    disagreements: List[Disagreement] = field(default_factory=list)


@dataclass
class BackfillPlan:
    """The whole run: what it would do, or (after `apply`) what it did."""
    since: datetime
    until: datetime
    prologue_since: datetime
    pipelines: List[PipelineBackfill] = field(default_factory=list)
    rules_applied: Dict[str, EpisodeGrouping] = field(default_factory=dict)
    # Distinct `config_fingerprint` values in range (ISSUE_85), and how many envelopes carry the
    # field at all. The second number is not decoration: the field landed 2026-08-16, so most of a
    # range reaching back to July carries none and the count of distinct values is a FLOOR. Reported
    # as "2 generations" over a 36-day range it read as "the configuration was stable", which is a
    # statement the data cannot make — the archive's known 2026-07-24 symbol expansion sits in the
    # blind window and would have moved the fingerprint had it existed.
    fingerprints: List[str] = field(default_factory=list)
    fingerprint_coverage: int = 0        # envelopes in range whose fingerprint is non-empty
    applied: bool = False
    envelopes_written: int = 0
    # Upserts issued, NOT rows created. An episode spanning 200 passes is upserted 200 times and
    # lands on one row — that is how `n_passes` accumulates. Reported as "9280 registry rows" for
    # 65 episodes it read like a duplication, so the two numbers are now separate and named.
    episode_upserts: int = 0
    episode_ids_written: int = 0

    @property
    def disagreements(self) -> List[Disagreement]:
        return [item for pipeline in self.pipelines for item in pipeline.disagreements]

    @property
    def would_stamp(self) -> int:
        return sum(pipeline.would_stamp for pipeline in self.pipelines)

    @property
    def carried(self) -> int:
        return sum(pipeline.carried for pipeline in self.pipelines)


@dataclass
class _Stamp:
    """One pending write: an envelope row, the result indices to touch, and its registry rows."""
    row_id: int
    pipeline_id: str
    # `[(result index, episode id, is_opener)]` — the index is the position in the stored `result`
    # array, which the parsed model preserves.
    fields: List[Tuple[int, str, bool]] = field(default_factory=list)
    episodes: List[EpisodeUpsert] = field(default_factory=list)


class EpisodeBackfill:
    """Replays a range through the live episode tracker and, on request, writes both sinks."""

    def __init__(self, database_url: str, groupings: Dict[str, EpisodeGrouping], *,
                 prologue: timedelta,
                 outcomes_table: str = 'outcomes',
                 registry: Optional[EpisodeRegistry] = None) -> None:
        self._database_url = database_url
        self._groupings = groupings
        self._prologue = prologue
        self._table = outcomes_table
        self._registry = registry if registry is not None else EpisodeRegistry()

    # --- public ---------------------------------------------------------------------------------

    def plan(self, since: datetime, until: datetime) -> BackfillPlan:
        """Replay and report. Writes nothing, ever — the default the CLI exposes."""
        plan, _ = self._replay(since, until)
        return plan

    def apply(self, since: datetime, until: datetime) -> BackfillPlan:
        """Replay, then write — unless the self-check disagreed anywhere.

        The abort is deliberate and has no override flag: a one-shot rewrite of an archive another
        project reads is not the place for `--force`. A disagreement is inspected, and either the
        range moves or the operator decides it is understood.
        """
        plan, stamps = self._replay(since, until)
        if plan.disagreements:
            logger.warning('[BACKFILL] refusing to write: %d disagreement(s) between the replay '
                           'and the served identities', len(plan.disagreements))
            return plan
        self._write(plan, stamps)
        return plan

    # --- replay ---------------------------------------------------------------------------------

    @staticmethod
    def _fresh(grouping: EpisodeGrouping) -> EpisodeGrouping:
        """A grouping with an EMPTY rule, rebuilt from the caller's parameters.

        `BreakingEpisodeRule` holds the open-episode state, and it lives on the grouping rather than
        on the tracker — so handing the caller's grouping to a second replay would drive it against
        the first replay's leftovers. Found by the idempotency test: after a `plan()`, an `apply()`
        on the same instance saw its opening pass as a continuation, because the rule still held the
        episode `plan()` had opened. The id was right and `breaking_episode_start` was silently
        false, which is precisely the half-field the consumer derives their `opened` edge from.

        Rebuilt from `get_exit_threshold()`/`get_gap()` rather than deep-copied: a copy would carry
        the state this exists to discard.
        """
        return EpisodeGrouping(
            BreakingEpisodeRule(exit_threshold=grouping.rule.get_exit_threshold(),
                                gap=grouping.rule.get_gap()),
            query_map=dict(grouping.query_map))

    def _replay(self, since: datetime,
                until: datetime) -> Tuple[BackfillPlan, List[_Stamp]]:
        """One read, one pass through the tracker, per pipeline in timestamp order."""
        prologue_since = since - self._prologue
        rows = self._read(prologue_since, until)
        plan = BackfillPlan(since=since, until=until, prologue_since=prologue_since,
                            rules_applied={key: self._fresh(value)
                                           for key, value in self._groupings.items()},
                            **self._fingerprints(since, until))
        stamps: List[_Stamp] = []
        per_pipeline: Dict[str, PipelineBackfill] = {}
        trackers: Dict[str, BreakingEpisodeTracker] = {}
        seen_episodes: Dict[str, Dict[str, None]] = {}

        for row_id, pipeline_id, raw in rows:
            envelope = SentimentEnvelope.model_validate(raw)
            if pipeline_id not in trackers:
                # An orphan pipeline in the archive (a retired stream) replays under the schema
                # defaults, exactly as the store reports treat one. Either way the rule is FRESH —
                # see `_fresh`.
                declared = self._groupings.get(pipeline_id)
                grouping = (self._fresh(declared) if declared is not None
                            else EpisodeGrouping(BreakingEpisodeRule()))
                trackers[pipeline_id] = BreakingEpisodeTracker(grouping)
            tracker = trackers[pipeline_id]

            if envelope.timestamp < since:
                # Prologue: state only. NOT stripped — an id already on the row is adopted, which
                # is what carries a story that opened before the range into it under its own
                # identity instead of a fresh one.
                tracker.observe(envelope)
                continue

            row = per_pipeline.setdefault(pipeline_id, PipelineBackfill(pipeline_id))
            row.envelopes += 1
            episodes_here = seen_episodes.setdefault(pipeline_id, {})
            stamp = _Stamp(row_id=row_id, pipeline_id=pipeline_id)

            # Strip before replaying, so `observe` COMPUTES instead of adopting. That is what makes
            # the served identities checkable: adopting them would compare a value against itself.
            served: List[Tuple[Optional[str], bool]] = [
                (result.breaking_episode_id, result.breaking_episode_start)
                for result in envelope.result]
            for result in envelope.result:
                result.breaking_episode_id = None
                result.breaking_episode_start = False

            outcome = tracker.observe(envelope)

            for index, result in enumerate(envelope.result):
                row.results += 1
                served_id, served_start = served[index]
                computed_id = result.breaking_episode_id
                computed_start = result.breaking_episode_start
                # Episodes are counted over every in-range row, carried or newly stamped, because
                # they describe the DATA. Counting only the stamped ones made them collapse to zero
                # on a re-run — a fully backfilled range reporting `episodes: 0` reads as "none
                # found". `would stamp` is the column that describes the action.
                if computed_id:
                    episodes_here.setdefault(computed_id, None)
                    if computed_start:
                        row.openers += 1
                if served_id:
                    row.carried += 1
                    if served_id != (computed_id or '') or served_start != computed_start:
                        row.disagreements.append(Disagreement(
                            pipeline_id=pipeline_id, row_id=row_id, ts=envelope.timestamp,
                            symbol=result.symbol, served_id=served_id,
                            computed_id=computed_id or '', served_start=served_start,
                            computed_start=computed_start))
                    continue
                if not computed_id:
                    continue                      # outside any episode — nothing to write
                row.would_stamp += 1
                stamp.fields.append((index, computed_id, computed_start))

            # The registry rows come from the tracker, already deduplicated per episode id rather
            # than per result (a fanned pair is several rows of ONE episode, ISSUE_70).
            if stamp.fields:
                stamp.episodes = list(outcome.episodes)
                stamps.append(stamp)

        for pipeline_id, row in per_pipeline.items():
            row.episodes = len(seen_episodes.get(pipeline_id, {}))
        plan.pipelines = [per_pipeline[key] for key in sorted(per_pipeline)]
        return plan, stamps

    # --- read -----------------------------------------------------------------------------------

    def _read(self, since: datetime, until: datetime) -> List[Tuple[int, str, object]]:
        """The range plus its prologue, ordered so the rule is driven as the live path drives it.

        `(pipeline_id, ts, id)`: contiguous per pipeline, ascending within — the rule is
        order-dependent, and `id` breaks a tie between two passes stamped in the same instant.
        `status <> 'error'` mirrors the store reports, so the rule sees the same population.
        The row `id` comes along because the write needs it and no model carries it.
        """
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT id, pipeline_id, envelope FROM {self._table} '
                    "WHERE ts >= %s AND ts < %s AND status <> 'error' "
                    'ORDER BY pipeline_id, ts, id',
                    (since, until))
                return cur.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f'backfill read failed: {exc}') from exc

    def _fingerprints(self, since: datetime, until: datetime) -> Dict[str, Any]:
        """Distinct `config_fingerprint` values in range, and how many envelopes carry one.

        One query for both, so the count and the coverage describe the same population. The
        coverage is what keeps the count honest: an envelope written before ISSUE_85 has an empty
        fingerprint, so over a range reaching back past 2026-08-16 the distinct count is a floor
        rather than a census.
        """
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cur.execute(
                    f"SELECT envelope->>'config_fingerprint', count(*) FROM {self._table} "
                    "WHERE ts >= %s AND ts < %s AND status <> 'error' "
                    'GROUP BY 1',
                    (since, until))
                rows = cur.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f'backfill fingerprint census failed: {exc}') from exc
        return {'fingerprints': sorted(value for value, _ in rows if value),
                'fingerprint_coverage': sum(count for value, count in rows if value)}

    # --- write ----------------------------------------------------------------------------------

    def _write(self, plan: BackfillPlan, stamps: List[_Stamp]) -> None:
        """Both sinks, one transaction per envelope.

        `jsonb_set` on the two keys of the indices that need them, never a whole-envelope rewrite:
        the invariant becomes provable rather than argued — nothing else in the envelope *can*
        change. `create_missing` is on because an envelope produced before ISSUE_65 has no such key
        to replace.
        """
        registered: Dict[str, None] = {}
        try:
            with psycopg.connect(self._database_url) as conn:
                for stamp in stamps:
                    with conn.cursor() as cur:
                        expression = 'envelope'
                        params: List[object] = []
                        for index, episode, opener in stamp.fields:
                            expression = (
                                f"jsonb_set(jsonb_set({expression}, "
                                f"'{{result,{index},breaking_episode_id}}', %s::jsonb, true), "
                                f"'{{result,{index},breaking_episode_start}}', %s::jsonb, true)")
                            params.extend([json.dumps(episode), json.dumps(opener)])
                        cur.execute(f'UPDATE {self._table} SET envelope = {expression} '
                                    'WHERE id = %s', (*params, stamp.row_id))
                        for episode_row in stamp.episodes:
                            self._registry.upsert(cur, episode_row)
                            plan.episode_upserts += 1
                            registered[episode_row.episode_id] = None
                        plan.envelopes_written += 1
                    conn.commit()
        except psycopg.Error as exc:
            raise VectorStoreError(f'backfill write failed: {exc}') from exc
        plan.applied = True
        plan.episode_ids_written = len(registered)
        logger.info('[BACKFILL] wrote %d envelope(s) · %d registry upsert(s) over %d episode(s)',
                    plan.envelopes_written, plan.episode_upserts, plan.episode_ids_written)


def _fmt_rules(rules: Dict[str, EpisodeGrouping]) -> str:
    """The grouping each pipeline was replayed under.

    Rendered here rather than reusing `breaking_report.format_rule_lines`: `core/outcome/` must not
    import from `core/observability/reports/` — a store unit depending on a reporting surface is the
    dependency the layout rule forbids, whichever direction reads better.
    """
    return ' · '.join(
        f'{pipeline_id} gap {int(grouping.rule.get_gap().total_seconds() // 60)}m '
        f'exit {grouping.rule.get_exit_threshold():.2f}'
        for pipeline_id, grouping in sorted(rules.items()))


def _fmt_census(plan: BackfillPlan) -> str:
    """The fingerprint census, stated as what it is: a floor, and a handover figure.

    Two readings had to be closed off. Over a range whose early days predate ISSUE_85 the distinct
    count is a lower bound, so the coverage travels with it. And a large count reads as alarming
    when it is not: `config_fingerprint` covers the source set, retrieval and the prompt, none of
    which the episode rule reads — the replay consumes the RECORDED `is_breaking`/`urgency` plus
    today's gap and exit gates. The number exists for the consumer's handover note, which is about
    the re-taken range's comparability, not about whether this replay is right.
    """
    envelopes = sum(row.envelopes for row in plan.pipelines)
    if not plan.fingerprints:
        return ('config generations in range: none recorded — every envelope here predates '
                'config_fingerprint (ISSUE_85)')
    floor = ' (a floor — the rest predate the field)' if plan.fingerprint_coverage < envelopes else ''
    return (f'config generations in range: {len(plan.fingerprints)} distinct, from '
            f'{plan.fingerprint_coverage} of {envelopes} envelopes that carry one{floor}. For the '
            f'handover note, not a replay input.\n'
            f'    {", ".join(plan.fingerprints)}')


def format_backfill_plan(plan: BackfillPlan) -> str:
    """Render as the shared console pattern: title, window line, `----` dividers, aligned columns."""
    # Width from the longest id present, never a constant: `crypto_sentiment_4o_enhanced` (28) ran
    # past a hard-coded 24 and shifted every column on its row — caught on the first real run.
    label_width = max([8] + [len(row.pipeline_id) for row in plan.pipelines])
    header = (f'{"pipeline":{label_width}} {"envelopes":>10} {"results":>9} {"would stamp":>12} '
              f'{"episodes":>9} {"openers":>8} {"carried":>8} {"disagree":>9}')
    divider = '-' * len(header)
    title = ('Episode identity backfill — APPLIED' if plan.applied
             else 'Episode identity backfill — DRY RUN, nothing written')
    prologue_hours = int((plan.since - plan.prologue_since).total_seconds() // 3600)
    lines = [
        title,
        f'range:    {plan.since:%Y-%m-%d %H:%M} → {plan.until:%Y-%m-%d %H:%M} UTC',
        f'prologue: {prologue_hours}h from {plan.prologue_since:%Y-%m-%d %H:%M} — state only, '
        f'never written',
        f'rules:    {_fmt_rules(plan.rules_applied) or "schema defaults"}',
        _fmt_census(plan),
        divider, header, divider,
    ]
    if not plan.pipelines:
        lines += ['(no envelopes in the range)', divider]
        return '\n'.join(lines)
    for row in plan.pipelines:
        lines.append(f'{row.pipeline_id:{label_width}} {row.envelopes:>10} {row.results:>9} '
                     f'{row.would_stamp:>12} {row.episodes:>9} {row.openers:>8} '
                     f'{row.carried:>8} {len(row.disagreements):>9}')
    lines.append(divider)
    lines.append(f'self-check: {plan.carried} result(s) already carried an id · '
                 f'{len(plan.disagreements)} disagreed with the replay')
    # Never omitted, even when the count is zero: the reader has to know the range is a
    # reconstruction whether or not this particular run found a discrepancy.
    lines.append('reconstruction, not recovery: 3 read-time policy changes landed inside the '
                 'consumer\'s window (2026-08-17 hysteresis, 08-18 gap 45→150, 08-18 episode '
                 'key) — FX episodes before 08-18 come out better than what was served, and '
                 'different from it')
    if plan.disagreements:
        lines.append(divider)
        lines.append(f'{len(plan.disagreements)} disagreement(s) — nothing was written, and '
                     f'--apply refuses until this is understood:')
        for item in plan.disagreements[:20]:
            lines.append(f'  {item.pipeline_id} {item.symbol} @ {item.ts:%Y-%m-%d %H:%M} '
                         f'(row {item.row_id})')
            lines.append(f'      served:   {item.served_id or "—"} '
                         f'(start={str(item.served_start).lower()})')
            lines.append(f'      computed: {item.computed_id or "—"} '
                         f'(start={str(item.computed_start).lower()})')
        if len(plan.disagreements) > 20:
            lines.append(f'  … {len(plan.disagreements) - 20} more not shown')
    elif plan.applied:
        lines.append(f'wrote {plan.envelopes_written} envelope(s) · '
                     f'{plan.episode_upserts} registry upsert(s) over '
                     f'{plan.episode_ids_written} episode(s) — an episode is upserted once per '
                     f'pass and lands on one row, which is how n_passes accumulates')
    else:
        lines.append('re-run with --apply to write (outcomes envelope JSONB + breaking_episodes)')
    return '\n'.join(lines)
