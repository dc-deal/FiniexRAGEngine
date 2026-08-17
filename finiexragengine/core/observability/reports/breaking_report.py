"""Breaking-detection report — reaction time + the flagged→confirmed funnel (ISSUE_11).

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

from finiexragengine.core.pipeline.breaking_episode_rule import BreakingEpisodeRule
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# Where an episode begins and ends is `BreakingEpisodeRule`'s decision, driven here exactly as the
# live tracker drives it — one implementation, two callers, so the dashboard and this report cannot
# diverge (they did, silently, for weeks when each grouped for itself). Rules are per pipeline
# because `breaking` is per-pipeline config; a pipeline_id present in the archive but no longer in
# config falls back to the schema defaults, the same orphan handling `sources_cli` uses.
PipelineRules = Dict[str, BreakingEpisodeRule]


@dataclass
class PipelineBreaking:
    """One pipeline's breaking episodes + their reaction-time samples, inside the window."""
    pipeline_id: str
    confirmed: int = 0                                    # breaking episodes
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


@dataclass
class BreakingReport:
    since_label: str
    rows: List[PipelineBreaking]
    flagged_candidates: int             # corpus breaking_candidate=TRUE in the window (all sets)
    confirmed_episodes: int
    episodes: List[BreakingEpisodeRow] = field(default_factory=list)   # per-episode listing (ISSUE_64)


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
                          outcomes_table: str = 'outcomes',
                          articles_table: str = 'articles',
                          rules: Optional[PipelineRules] = None) -> BreakingReport:
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
                return BreakingReport(since_label, [], 0, 0)
            cur.execute(
                f'SELECT pipeline_id, envelope FROM {outcomes_table} '
                "WHERE ts >= %s AND status <> 'error' ORDER BY pipeline_id, ts",
                (since,))
            rows = cur.fetchall()
            # Flagged candidates in the corpus within the window (shared across a set's pipelines).
            flagged = 0
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (articles_table,))
            if cur.fetchone()[0]:
                cur.execute(
                    f'SELECT count(*) FROM {articles_table} '
                    'WHERE breaking_candidate = TRUE AND flagged_at >= %s', (since,))
                flagged = int(cur.fetchone()[0])
    except psycopg.Error as exc:
        raise VectorStoreError(f'breaking report failed: {exc}') from exc

    return _aggregate(rows, flagged, since_label, rules or {})


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
               rules: Optional[PipelineRules] = None) -> BreakingReport:
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
    engines: Dict[str, BreakingEpisodeRule] = {}
    # The episode row currently open per (pipeline, asset) — continuations grow it in place.
    open_rows: Dict[Tuple[str, str], BreakingEpisodeRow] = {}

    for pipeline_id, t3, env in parsed:
        rule = engines.setdefault(pipeline_id, rules.get(pipeline_id) or BreakingEpisodeRule())
        for result in env.get('result', []):
            # Keyed on the asset (base_currency), not the ticker — a query group's fanned symbols
            # (ETHUSD/ETHEUR, both base ETH) are one analysis → one episode, mirroring the live
            # tracker (ISSUE_70); falls back to the symbol for pre-#70 envelopes.
            group_key = result.get('base_currency') or result['symbol']
            decision = rule.observe(group_key, t3, bool(result.get('is_breaking')),
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
                                             result.get('reasoning', ''), engine, end_to_end)
                episodes.append(current)
                open_rows[(pipeline_id, group_key)] = current
            elif decision.held:
                current = open_rows.get((pipeline_id, group_key))
                if current is not None:
                    # Duration runs to the last QUALIFYING pass, not to a dip that merely happened
                    # before the gap elapsed — otherwise a story's length would include its silence.
                    current.duration_s = (t3 - current.started).total_seconds()

    ordered = sorted(per_pipeline.values(), key=lambda row: row.pipeline_id)
    # Group the listing by pipeline THEN symbol (then time): a symbol's episodes cluster, so signal
    # consistency (all BUY vs a BUY→SELL flip) is scannable at a glance (ISSUE_64 feedback).
    episodes.sort(key=lambda episode: (episode.pipeline_id, episode.symbol, episode.started))
    return BreakingReport(since_label, ordered, flagged,
                          sum(row.confirmed for row in ordered), episodes)


def _fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return '—'
    return f'{seconds:.0f}s' if seconds < 90 else f'{seconds / 60:.1f}m'


def _fmt_pair(values: List[float]) -> str:
    median = _percentile(values, 0.5)
    if median is None:
        return '—'
    return f'{_fmt_seconds(median)} / {_fmt_seconds(_percentile(values, 0.9))}'


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
        'Breaking Detection — reaction & funnel',
        f'window: last {report.since_label}',
        divider,
        f'{"pipeline":24} {"confirmed":>9}  {"engine react":>15}  {"end-to-end":>15}',
        f'{"":24} {"episodes":>9}  {"med / p90":>15}  {"med / p90":>15}',
        divider,
    ]
    for row in report.rows:
        lines.append(f'{row.pipeline_id:24} {row.confirmed:>9}  '
                     f'{_fmt_pair(row.engine_reaction_s):>15}  {_fmt_pair(row.end_to_end_s):>15}')
    if not report.rows:
        lines.append('(no confirmed breaking in the window)')
    lines.append(divider)
    # The funnel: flagged (corpus, LLM-free) → confirmed (LLM) → pushed (live channel, Stage C).
    lines.append(f'funnel: {report.flagged_candidates} flagged → '
                 f'{report.confirmed_episodes} confirmed → push (Stage C, pending)')
    lines.append('engine react = t3−freshest fetched_at (what we control) · '
                 'end-to-end = t3−freshest published_at (what the consumer feels)')

    # Per-episode listing (ISSUE_64): what broke this window, grouped by pipeline — when it started,
    # how long it lasted, and why (the LLM's reasoning). Edge-triggered, so one line per real episode.
    lines.append('')
    lines.append(f'Breaking episodes — last {report.since_label}')
    lines.append(divider)
    lines.append(f'  {"symbol":8} {"sig":4} {"started":>11}  {"dur":>6}  why')
    if not report.episodes:
        lines.append('  (none)')
    else:
        # The fixed prefix is 37 cells (`  {sym:8} {sig:4} {started:>11}  {dur:>6}  `); the reason
        # fills whatever the console has left, minus a 5-cell safety margin so a slightly-miscounted
        # width (or a console that reports one column too many) never wraps a line (ISSUE_64 feedback).
        reason_budget = max(20, term_width - 37 - 5)
        current_pipeline: Optional[str] = None
        current_symbol: Optional[str] = None
        for episode in report.episodes:
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
            lines.append(f'  {symbol_cell:8} {episode.signal:4} {started:>11}  '
                         f'{duration:>6}  {_truncate(episode.reason, reason_budget)}')
    return '\n'.join(lines)
