"""Breaking-detection report — reaction time, episodes and the stories behind them (ISSUE_11).

Aggregated **from the store**, never from logs: the persisted envelopes are the source of truth
(CLAUDE.md — capture at the call, report from the store). Reaction time is a live measurement that
cannot be rebuilt after the fact, so it rides on fields captured at the event: the envelope's
`timestamp` (t3), each source's `published_at` (t0) and `fetched_at` (t1). The detector's
`flagged_at` lives in the corpus and feeds the funnel's numerator.

Both reaction times anchor on the **freshest** source of an episode, not the oldest (ISSUE_81) —
the oldest is bounded by the retrieval window, so it measured "how far back did we read" rather
than "how fast did we react". Recomputing from persisted envelopes means the correction applies to
the whole history, not just to runs after the fix. The same is true of the episode rule below: it
is applied at read time, so retuning it re-groups the whole archive rather than only the future.
"""
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import psycopg

from finiexragengine.core.pipeline.breaking_story_rule import (
    StoryCandidate,
    StoryGrouping,
    assign_stories,
)
from finiexragengine.core.pipeline.breaking_episode_rule import (
    BreakingEpisodeRule,
    EpisodeGrouping,
)
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.core.pipeline.detection_preflight import (
    format_reachability_lines,
    with_quarantine,
)
from finiexragengine.types.ingest_types import DetectionReachability

# Where an episode begins and ends, and what it is keyed by, is `EpisodeGrouping`'s decision —
# driven here exactly as the live tracker drives it. One implementation, two callers, so the
# dashboard and this report cannot diverge (they did, silently, for weeks when each grouped for
# itself). Groupings are per pipeline because `breaking` and the symbol table both are; a
# pipeline_id present in the archive but no longer in config falls back to the schema defaults,
# the same orphan handling `sources_cli` uses.
PipelineGroupings = Dict[str, EpisodeGrouping]


@dataclass
class PipelineBreaking:
    """One pipeline's breaking episodes + their reaction-time samples, inside the window."""
    pipeline_id: str
    confirmed: int = 0                                    # breaking episodes
    stories: int = 0                                      # distinct news behind them (ISSUE_96)
    engine_reaction_s: List[float] = field(default_factory=list)   # t3 − freshest fetched_at
    end_to_end_s: List[float] = field(default_factory=list)        # t3 − freshest published_at


@dataclass
class BreakingEpisodeRow:
    """One confirmed episode in the window — for the per-episode listing (ISSUE_64): when it started,
    how long it lasted, and why (the LLM's `reasoning`, frozen at the episode start like the reaction
    time). A report-local shape, built and consumed here (CLAUDE.md — self-contained unit)."""
    pipeline_id: str
    symbol: str
    signal: str
    started: datetime
    duration_s: float                # last breaking pass − start (0 = single-pass episode)
    reason: str
    engine_s: Optional[float]
    end_to_end_s: Optional[float]
    # The model's purpose-built breaking line (ISSUE_64 Phase 2, prompt v3), empty before it.
    # Beside `reason`, never instead of it: `reason` is what the story measure clusters on, and
    # its threshold was calibrated over `reasoning` texts (see `breaking_episode.BreakingEpisode`).
    breaking_reason: str = ''
    # Which news this episode belongs to (ISSUE_96) — episodes sharing one are the same story,
    # re-derived at read time from the `reason` text. Numbered per pipeline in reading order.
    story_id: int = 0

    @property
    def display_reason(self) -> str:
        """What the listing prints — the purpose-built line, else the signal's `reasoning`."""
        return self.breaking_reason or self.reason


@dataclass
class BreakingReport:
    since_label: str
    rows: List[PipelineBreaking]
    flagged_candidates: int             # corpus breaking_candidate=TRUE in the window (all sets)
    confirmed_episodes: int
    episodes: List[BreakingEpisodeRow] = field(default_factory=list)   # per-episode listing (ISSUE_64)
    total_stories: int = 0              # distinct news across every pipeline (ISSUE_96)
    # The rule each pipeline was actually grouped with. A report that re-derives history at read
    # time has to say under which rule, or two runs of the same command over the same archive are
    # silently incomparable — the `[OVERRIDE]` startup line only shows up when an override exists.
    rules_applied: Dict[str, EpisodeGrouping] = field(default_factory=dict)
    # The story rule each pipeline was grouped with, for the same reason as `rules_applied`:
    # a read-time re-derivation that does not name its rule is not reproducible.
    stories_applied: Dict[str, StoryGrouping] = field(default_factory=dict)
    # Whether each source-set's detection thresholds can still fire (ISSUE_106). This is the report
    # an operator opens when asking "why is nothing flagging", so the answer "because the threshold
    # is out of reach for the feeds that are running" belongs here rather than only in a boot line
    # nobody scrolls back to. Resolved by the caller from the registry factory — the only load path
    # that honours the `user_configs/` overlay, which is what moves these counts per machine.
    reachability: List[DetectionReachability] = field(default_factory=list)
    # `flagged_candidates` split by the path that raised the tier (ISSUE_106) — the number that
    # makes either threshold tunable, where the total never could. An `unrecorded` key means those
    # rows were flagged before the column existed; it is deliberately not folded into either path.
    by_trigger: Dict[str, int] = field(default_factory=dict)


# What a NULL `detection_trigger` renders as: flagged before the column existed.
_UNRECORDED = 'unrecorded'


def _parse_dt(value: str) -> datetime:
    # Pydantic serializes tz-aware ISO 8601; tolerate a trailing 'Z' too.
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (0..1) — None on an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def build_breaking_report(database_url: str, since: datetime, *, since_label: str = '7d',
                         stories: Optional[Dict[str, StoryGrouping]] = None,
                          outcomes_table: str = 'outcomes',
                          articles_table: str = 'articles',
                          health_table: str = 'source_health',
                          rules: Optional[PipelineGroupings] = None,
                          reachability: Optional[List[DetectionReachability]] = None
                          ) -> BreakingReport:
    """Aggregate confirmed breaking episodes + reaction times + the corpus flag count.

    `rules` carries each pipeline's episode rule, resolved by the caller from the registry
    factories (the only load path that honours the `user_configs/` overlay). Omitted = schema
    defaults for every pipeline, which is what a caller without a config context should get.
    """
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # No outcomes table yet = nothing produced; a clean empty report, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (outcomes_table,))
            if cur.fetchone()[0] == 0:
                # `by_trigger` is EMPTY here, never `dict(by_trigger or {})`: this function takes
                # no such parameter, and the name is bound further down (the corpus census). The
                # copied `or {}` idiom belongs to `_aggregate`, which does take one — here it made
                # the name function-local and turned this early return into an UnboundLocalError.
                # The branch whose whole purpose is "a clean empty report, not a crash" was the
                # crash, on any database without an outcomes table.
                return BreakingReport(since_label, [], 0, 0,
                                      reachability=list(reachability or []),
                                      by_trigger={})
            cur.execute(
                f'SELECT pipeline_id, envelope FROM {outcomes_table} '
                "WHERE ts >= %s AND status <> 'error' ORDER BY pipeline_id, ts",
                (since,))
            rows = cur.fetchall()
            # Flagged candidates in the corpus within the window (shared across a set's pipelines).
            flagged = 0
            by_trigger: Dict[str, int] = {}
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (articles_table,))
            if cur.fetchone()[0]:
                # Split by the path that raised the tier (ISSUE_106). The column is guarded rather
                # than assumed: a database where migration 011 has not run must still produce a
                # report, and the honest answer there is that the split is unavailable — not that
                # both paths fired zero times.
                cur.execute('SELECT count(*) FROM information_schema.columns '
                            'WHERE table_name = %s AND column_name = %s',
                            (articles_table, 'detection_trigger'))
                if cur.fetchone()[0]:
                    cur.execute(
                        f'SELECT coalesce(detection_trigger, %s), count(*) FROM {articles_table} '
                        'WHERE breaking_candidate = TRUE AND flagged_at >= %s '
                        'GROUP BY 1', (_UNRECORDED, since))
                    by_trigger = {trigger: int(count) for trigger, count in cur.fetchall()}
                    flagged = sum(by_trigger.values())
                else:
                    cur.execute(
                        f'SELECT count(*) FROM {articles_table} '
                        'WHERE breaking_candidate = TRUE AND flagged_at >= %s', (since,))
                    flagged = int(cur.fetchone()[0])
            # Re-state each set's threshold verdict against the feeds that are pollable RIGHT NOW
            # (ISSUE_106). Quarantine is dynamic, so the boot preflight cannot know it and this
            # report must not inherit its number — the two are honestly different, and each says
            # which it is. Guarded: a database without the health table yields the config-time
            # verdict, marked as such, rather than a live-looking claim nobody measured.
            live: List[DetectionReachability] = list(reachability or [])
            if live:
                cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                            (health_table,))
                if cur.fetchone()[0]:
                    cur.execute(f'SELECT source_id FROM {health_table} '
                                'WHERE quarantined_until > now()')
                    quarantined = {row[0] for row in cur.fetchall()}
                    live = [with_quarantine(reach, quarantined) for reach in live]
    except psycopg.Error as exc:
        raise VectorStoreError(f'breaking report failed: {exc}') from exc

    return _aggregate(rows, flagged, since_label, rules or {}, stories=stories,
                      reachability=live, by_trigger=by_trigger)


def _reaction(result: Dict[str, object], t3: datetime) -> Tuple[Optional[float], Optional[float]]:
    """`(engine_s, end_to_end_s)` for one result — the dict-side twin of
    `breaking_episode.reaction_times`, which does the same arithmetic on the typed model.

    Anchored on the FRESHEST source (ISSUE_81 — see the live twin for why the oldest measured the
    retrieval window instead of a reaction). Estimated publish dates (a date-less feed falls back
    `published := fetched`) are excluded from e2e so it does not collapse onto engine.
    """
    sources = result.get('sources', []) or []
    published = [_parse_dt(s['published_at']) for s in sources
                 if s.get('published_at') and s['published_at'] != s.get('fetched_at')]
    fetched = [_parse_dt(s['fetched_at']) for s in sources if s.get('fetched_at')]
    engine = (t3 - max(fetched)).total_seconds() if fetched else None
    end_to_end = (t3 - max(published)).total_seconds() if published else None
    return engine, end_to_end


def _aggregate(rows: List[Tuple[str, object]], flagged: int, since_label: str,
               rules: Optional[PipelineGroupings] = None,
               stories: Optional[Dict[str, StoryGrouping]] = None,
               reachability: Optional[List[DetectionReachability]] = None,
               by_trigger: Optional[Dict[str, int]] = None) -> BreakingReport:
    """Group passes into episodes + reaction samples — the DB-free core (tested).

    Drives `BreakingEpisodeRule` exactly as the live tracker does. Note that **every** result is
    observed, not only the breaking ones: under hysteresis a pass below the confirm gate but at or
    above the exit gate is what holds a story open, so skipping non-breaking rows would close
    episodes early and re-open them — the ~4.7x overcount ISSUE_82 measured.
    """
    rules = rules or {}
    # The rule is order-dependent, so parse and sort up front rather than trusting the caller's
    # ordering. The SQL above already returns (pipeline_id, ts); a test building rows by hand
    # should not have to know that.
    parsed: List[Tuple[str, datetime, Dict[str, object]]] = []
    for pipeline_id, envelope in rows:
        env = envelope if isinstance(envelope, dict) else json.loads(envelope)
        parsed.append((pipeline_id, _parse_dt(env['timestamp']), env))
    parsed.sort(key=lambda item: (item[0], item[1]))

    per_pipeline: Dict[str, PipelineBreaking] = {}
    episodes: List[BreakingEpisodeRow] = []
    engines: Dict[str, EpisodeGrouping] = {}
    # The episode row currently open per (pipeline, asset) — continuations grow it in place.
    open_rows: Dict[Tuple[str, str], BreakingEpisodeRow] = {}

    for pipeline_id, t3, env in parsed:
        grouping = engines.setdefault(
            pipeline_id, rules.get(pipeline_id) or EpisodeGrouping(BreakingEpisodeRule()))
        for result in env.get('result', []):
            # Keyed on the retrieval query — the analysis unit, mirroring the live tracker exactly
            # (see `EpisodeGrouping.key_for`).
            group_key = grouping.key_for(result['symbol'], result.get('base_currency'))
            decision = grouping.rule.observe(group_key, t3, bool(result.get('is_breaking')),
                                             float(result.get('urgency') or 0.0))
            if decision.opened:
                # Reaction time and reason are sampled only at the edge — later confirmations of
                # the same story do not reset them; continuations only extend the duration.
                engine, end_to_end = _reaction(result, t3)
                # Created on the first EPISODE, not on the first pass: a pipeline that produced
                # passes but never broke stays out of the funnel table, as before ISSUE_82.
                row = per_pipeline.setdefault(pipeline_id, PipelineBreaking(pipeline_id))
                row.confirmed += 1
                if engine is not None:
                    row.engine_reaction_s.append(engine)
                if end_to_end is not None:
                    row.end_to_end_s.append(end_to_end)
                current = BreakingEpisodeRow(pipeline_id, result['symbol'],
                                             result.get('signal', ''), t3, 0.0,
                                             result.get('reasoning', ''), engine, end_to_end,
                                             breaking_reason=result.get('breaking_reason') or '')
                episodes.append(current)
                open_rows[(pipeline_id, group_key)] = current
            elif decision.held:
                current = open_rows.get((pipeline_id, group_key))
                if current is not None:
                    # Duration runs to the last QUALIFYING pass, not to a dip that merely happened
                    # before the gap elapsed — otherwise a story's length would include its silence.
                    current.duration_s = (t3 - current.started).total_seconds()

    # Stories are assigned AFTER every episode exists (ISSUE_96): the measure needs the whole
    # window's reasons to learn which words are boilerplate, so it cannot run inside the pass loop.
    # The episode rule above is untouched by it — one derivation feeds the next, never the reverse.
    story_rules = stories or {}
    story_engines: Dict[str, StoryGrouping] = {}
    for pipeline_id in {episode.pipeline_id for episode in episodes}:
        grouping = story_rules.get(pipeline_id) or StoryGrouping()
        story_engines[pipeline_id] = grouping
        mine = [episode for episode in episodes if episode.pipeline_id == pipeline_id]
        # Deliberately `reason` (the LLM's `reasoning`), never `breaking_reason`: `story_similarity`
        # was measured over 1,455 real `reasoning` texts, and `breaking_reason` is empty on every
        # envelope produced before prompt v3. Pointing this at the new field would retire that
        # calibration without anyone noticing.
        candidates = [StoryCandidate(key=episode.symbol, started=episode.started,
                                     reason=episode.reason) for episode in mine]
        for episode, story_id in zip(mine, assign_stories(candidates, grouping)):
            episode.story_id = story_id
        # A pipeline that produced episodes always has a row here — it was created on the first one.
        per_pipeline[pipeline_id].stories = len({episode.story_id for episode in mine})

    ordered = sorted(per_pipeline.values(), key=lambda row: row.pipeline_id)
    # Group the listing by pipeline THEN symbol (then time): a symbol's episodes cluster, so signal
    # consistency (all BUY vs a BUY→SELL flip) is scannable at a glance (ISSUE_64 feedback).
    episodes.sort(key=lambda episode: (episode.pipeline_id, episode.symbol, episode.started))
    return BreakingReport(since_label, ordered, flagged,
                          sum(row.confirmed for row in ordered), episodes,
                          sum(row.stories for row in ordered), engines, story_engines,
                          reachability=list(reachability or []),
                          by_trigger=dict(by_trigger or {}))


def _fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return '—'
    return f'{seconds:.0f}s' if seconds < 90 else f'{seconds / 60:.1f}m'


def _fmt_pair(values: List[float]) -> str:
    median = _percentile(values, 0.5)
    if median is None:
        return '—'
    return f'{_fmt_seconds(median)} / {_fmt_seconds(_percentile(values, 0.9))}'


def format_story_lines(stories_applied: Dict[str, StoryGrouping]) -> List[str]:
    """The story rule each pipeline was grouped with (ISSUE_96) — the episode rule's twin.

    Same reasoning: the story count is a read-time re-derivation over the `reason` texts, so a page
    that prints it without naming the rule cannot be compared against another run.
    """
    if not stories_applied:
        return []
    ordered = sorted(stories_applied.items())
    if len(ordered) == 1:
        pipeline_id, grouping = ordered[0]
        return [f'story rule (read-time): {pipeline_id} {grouping.describe()}']
    width = max(len(pipeline_id) for pipeline_id in stories_applied)
    return ['story rule (read-time):'] + [
        f'  {pipeline_id:{width}}  {grouping.describe()}' for pipeline_id, grouping in ordered]


def format_rule_lines(rules_applied: Dict[str, EpisodeGrouping]) -> List[str]:
    """The episode rule each pipeline was grouped with, as header lines (ISSUE_82).

    Both breaking surfaces render this, because both re-derive the archive at read time: without it
    two runs of the same command over the same data can differ and nothing on the page says why.
    The **open** gate is deliberately absent — an episode opens on the `is_breaking` recorded at the
    time, which may have been taken under a different `urgency_threshold` than today's config, so
    printing one would misdescribe the history.
    """
    if not rules_applied:
        return []
    def _render(grouping: EpisodeGrouping) -> str:
        return (f'hold ≥{grouping.rule.get_exit_threshold():.2f} · '
                f'gap {int(grouping.rule.get_gap().total_seconds() // 60)}m')

    ordered = sorted(rules_applied.items())
    if len(ordered) == 1:
        pipeline_id, grouping = ordered[0]
        return [f'episode rule (read-time): {pipeline_id} {_render(grouping)}']
    width = max(len(pipeline_id) for pipeline_id in rules_applied)
    return ['episode rule (read-time):'] + [
        f'  {pipeline_id:{width}}  {_render(grouping)}' for pipeline_id, grouping in ordered]


def _story_mark(episodes: List[BreakingEpisodeRow], index: int) -> str:
    """`┐ ├ ┘` for an episode that shares its story with a neighbour, blank when it stands alone.

    Drawn from the neighbours rather than stored on the row: the listing is already sorted by
    (pipeline, symbol, time), so a story's episodes are adjacent by construction and the bracket
    only has to know whether the row above and below belong to the same one.
    """
    episode = episodes[index]
    same = lambda other: (other.pipeline_id == episode.pipeline_id
                          and other.symbol == episode.symbol
                          and other.story_id == episode.story_id)
    above = index > 0 and same(episodes[index - 1])
    below = index + 1 < len(episodes) and same(episodes[index + 1])
    if above and below:
        return '├'
    if below:
        return '┐'
    if above:
        return '┘'
    return ' '


def _truncate(text: str, budget: int) -> str:
    """Cut `text` to `budget` cells, ellipsis-marked — so a long reason never overruns the console."""
    return text if len(text) <= budget else text[:budget - 1] + '…'


def format_breaking_report(report: BreakingReport, *, width: Optional[int] = None) -> str:
    """Render the report as the shared console pattern (no per-run footer — this is an aggregate).

    `width` (default: the live terminal via `shutil.get_terminal_size`, cross-shell, 80 when piped)
    caps the reason column so a long line adapts to the console instead of a fixed cut.
    """
    term_width = width if width is not None else shutil.get_terminal_size((80, 24)).columns
    divider = '-' * 72
    lines = [
        'Breaking Detection — reaction & stories',
        f'window: last {report.since_label}',
        *format_rule_lines(report.rules_applied),
        *format_story_lines(report.stories_applied),
        divider,
        f'{"pipeline":24} {"confirmed":>9} {"stories":>7}  {"engine react":>15}  {"end-to-end":>15}',
        f'{"":24} {"episodes":>9} {"":>7}  {"med / p90":>15}  {"med / p90":>15}',
        divider,
    ]
    for row in report.rows:
        lines.append(f'{row.pipeline_id:24} {row.confirmed:>9} {row.stories:>7}  '
                     f'{_fmt_pair(row.engine_reaction_s):>15}  {_fmt_pair(row.end_to_end_s):>15}')
    if not report.rows:
        lines.append('(no confirmed breaking in the window)')
    lines.append(divider)
    # Three counts of the same window, NOT a funnel — the arrow was removed deliberately
    # (ISSUE_82 step 4, ISSUE_96). Detection and confirmation are near-independent channels:
    # both XRPUSD episodes of 2026-08-17 came from articles the detector never flagged, and
    # breaking-triggered passes carry an `is_breaking` row no more often than scheduled ones
    # (30.8 % vs 40.5 %). Reading `flagged → confirmed` as a yield is therefore wrong, and an
    # arrow is what invited it. `over N stories` is the number the episode count should be read with.
    lines.append(f'{report.flagged_candidates} flagged (corpus) · '
                 f'{report.confirmed_episodes} confirmed episodes over '
                 f'{report.total_stories} stories · push pending (Stage C)')
    lines.append('flagged and confirmed are independent channels, not a yield — '
                 'a flagged article often is not confirmed, and most episodes were never flagged')
    # WHICH path did the flagging (ISSUE_106). The total above cannot tune either threshold; this
    # can. `unrecorded` is shown as its own bucket, never folded into a path — those rows were
    # flagged before the column existed and their decision is irreconstructable.
    if report.by_trigger:
        split = ' · '.join(f'{count} {trigger}'
                           for trigger, count in sorted(report.by_trigger.items()))
        lines.append(f'flagged by path: {split}')
        if _UNRECORDED in report.by_trigger:
            lines.append(f'  {_UNRECORDED} = flagged before the detection trigger was persisted '
                         '(migration 011) — not attributable to either path, and never backfilled')
    lines.append('engine react = t3−freshest fetched_at (what we control) · '
                 'end-to-end = t3−freshest published_at (what the consumer feels)')

    # Can the thresholds behind that flagged count still fire? (ISSUE_106) The census renders
    # whether or not anything is wrong — "nothing was reported" has to be distinguishable from
    # "nothing was checked" — and the wording is the boot preflight's own function, so the two
    # surfaces cannot describe the same state differently. NOTE the numbers here are the *enabled*
    # counts: quarantine is dynamic and belongs to `source_health`, which reads it at read time.
    if report.reachability:
        lines.append(divider)
        unsatisfiable = [r for r in report.reachability if not r.satisfiable]
        lines.append(f'detection reachability: {len(report.reachability)} source-set(s) checked · '
                     + (f'{len(unsatisfiable)} with a path out of reach'
                        if unsatisfiable else 'all thresholds satisfiable'))
        for reach in report.reachability:
            for line in format_reachability_lines(reach):
                lines.append(f'  {line}')
        if unsatisfiable:
            lines.append('  a threshold nothing can reach fires never and reports nothing — '
                         'which reads exactly like a quiet news week')

    # Per-episode listing (ISSUE_64): what broke this window, grouped by pipeline — when it started,
    # how long it lasted, and why (the LLM's reasoning). Edge-triggered, so one line per real episode.
    lines.append('')
    lines.append(f'Breaking episodes — last {report.since_label}')
    lines.append(divider)
    lines.append(f'  {"symbol":8} {"sig":4} {"started":>11}  {"dur":>6}    why')
    if not report.episodes:
        lines.append('  (none)')
    else:
        # The fixed prefix is 38 cells (`  {sym:8} {sig:4} {started:>11}  {dur:>6} {story:1} `) —
        # 37 before ISSUE_96 added the story bracket. The reason fills whatever the console has
        # left, minus a 5-cell safety margin so a slightly-miscounted width (or a console that
        # reports one column too many) never wraps a line (ISSUE_64 feedback).
        reason_budget = max(20, term_width - 38 - 5)
        current_pipeline: Optional[str] = None
        current_symbol: Optional[str] = None
        for index, episode in enumerate(report.episodes):
            if episode.pipeline_id != current_pipeline:
                lines.append(episode.pipeline_id)          # section header per pipeline
                current_pipeline = episode.pipeline_id
                current_symbol = None                      # reset the symbol grouping under it
            # Show the symbol once per group (blank on repeats), so a symbol's rows read as a block
            # and its signal column (BUY/SELL) is scannable for consistency (ISSUE_64 feedback).
            symbol_cell = episode.symbol if episode.symbol != current_symbol else ''
            current_symbol = episode.symbol
            started = episode.started.strftime('%m-%d %H:%M')
            duration = _fmt_seconds(episode.duration_s) if episode.duration_s else '—'
            # One cell marking which episodes the story rule put together (ISSUE_96), so the
            # grouping can be READ rather than trusted — a re-derived number nobody can check is
            # the thing the story measure exists to replace. Blank when an episode stands alone.
            lines.append(f'  {symbol_cell:8} {episode.signal:4} {started:>11}  '
                         f'{duration:>6} {_story_mark(report.episodes, index):1} '
                         f'{_truncate(episode.display_reason, reason_budget)}')
    return '\n'.join(lines)
