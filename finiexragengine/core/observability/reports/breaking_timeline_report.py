"""Breaking state timeline — the on/off series behind the episode count (ISSUE_82).

The funnel report answers "how many episodes"; this one answers "and should you believe that
number". It renders the window as a strip of cells, so the shape of the series is visible: a clean
block is one story, a comb is a threshold being crossed back and forth by noise.

That distinction is the whole reason ISSUE_82 exists. `urgency` is quantised to seven values and
the confirm gate sits on one of them, so a mean pass-to-pass drift of 0.032 — a third of a lattice
step, on a byte-identical source set — was enough to turn ~14 stories into 66 episodes in a week.
The flip count here is that noise, measured; the episode count is what the rule makes of it. After
the hysteresis rule the flips must stay (the model is untouched) while the episodes collapse — so
this report is also the acceptance measurement for the change that created it.

Three things it deliberately does NOT hide, each of which cost a question the first time it ran
against production:

- **Every configured symbol gets a row**, including the ones that never broke. "No line" used to
  mean three different things — never broke, never scored (a mechanical `no_data` HOLD, no LLM
  call), or not configured at all — and the reader could not tell which.
- **Rows are the rule's own grouping**, not one row per ticker. A fanned pair (ETHUSD/ETHEUR, one
  analysis under ISSUE_70) is one episode, so showing it as two rows put all the episodes on one
  and a bare `0` on its twin, and doubled both totals.
- **The strip spans the window** by bucketing, never by truncation. Cutting 1079 passes to the
  ~52 cells a console affords showed the last nine hours under a header that said "last 7d".

Read from the store like every other surface here, so it re-derives the whole history under
whatever rule is configured today rather than only describing the future.
"""
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import psycopg

from finiexragengine.core.observability.reports.breaking_report import PipelineRules
from finiexragengine.core.pipeline.breaking_episode_rule import BreakingEpisodeRule
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# The cell alphabet. The first three are what the rule distinguishes, so the strip reads as the
# rule's own view rather than as a second interpretation of it. The last two are absences, and they
# are kept apart on purpose: a symbol the engine did not score (retrieval empty after the floor) is
# not an engine that produced nothing. Collapsing them made a calm symbol look like an outage.
_CELL_BREAKING = '#'      # at or above the confirm gate — `is_breaking` as recorded
_CELL_HELD = '.'          # below it but at or above the exit gate — the hold band
_CELL_BELOW = '_'         # below both — the gap clock is running (or nothing is open)
_CELL_MECHANICAL = '-'    # the pass ran but this symbol was not scored (`basis != 'llm'`)
_CELL_NO_PASS = '~'       # no pass at all landed in this bucket — an outage
# Strongest state wins when a bucket holds several passes: one breaking pass in three hours is the
# thing you are looking for, and averaging it away would defeat the report.
_PRECEDENCE = {_CELL_BREAKING: 4, _CELL_HELD: 3, _CELL_BELOW: 2,
               _CELL_MECHANICAL: 1, _CELL_NO_PASS: 0}


@dataclass
class SymbolTimeline:
    """One analysis unit's pass series inside the window, with what the rule made of it.

    `symbols` is a list because the rule groups fanned tickers into one episode (ISSUE_70): the row
    is the unit the rule actually reasons about, and the label names every ticker in it.
    """
    pipeline_id: str
    symbols: List[str] = field(default_factory=list)
    samples: List[Tuple[datetime, str]] = field(default_factory=list)   # (ts, cell), oldest first
    passes: int = 0                   # LLM-scored passes — the ones that are evidence
    mechanical: int = 0               # `basis != 'llm'`: retrieval was empty, no LLM call was made
    breaking_passes: int = 0
    flips: int = 0                    # verdict changes — the noise this issue is about
    episodes: int = 0                 # what the rule grouped them into
    first_breaking: Optional[datetime] = None
    last_breaking: Optional[datetime] = None

    def label(self) -> str:
        return '/'.join(self.symbols)


@dataclass
class BreakingTimelineReport:
    since_label: str
    rows: List[SymbolTimeline] = field(default_factory=list)
    symbol_filter: str = ''
    since: Optional[datetime] = None     # window bounds, so the strip can span them honestly
    until: Optional[datetime] = None


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def build_breaking_timeline_report(database_url: str, since: datetime, *,
                                   since_label: str = '7d',
                                   symbol: str = '',
                                   outcomes_table: str = 'outcomes',
                                   rules: Optional[PipelineRules] = None,
                                   ) -> BreakingTimelineReport:
    """The per-unit on/off series over the window, grouped by the configured episode rule."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (outcomes_table,))
            if cur.fetchone()[0] == 0:
                return BreakingTimelineReport(since_label, [], symbol)
            cur.execute(
                f'SELECT pipeline_id, envelope FROM {outcomes_table} '
                "WHERE ts >= %s AND status <> 'error' ORDER BY pipeline_id, ts",
                (since,))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise VectorStoreError(f'breaking timeline report failed: {exc}') from exc
    # `until` is wall-clock, not the last envelope: a strip that stops where the data stops would
    # hide the very outage its `~` cells exist to show.
    return _aggregate_timeline(rows, since_label, symbol, rules or {},
                               since=since, until=datetime.now(timezone.utc))


def _aggregate_timeline(rows: List[Tuple[str, object]], since_label: str, symbol_filter: str,
                        rules: PipelineRules, *, since: Optional[datetime] = None,
                        until: Optional[datetime] = None) -> BreakingTimelineReport:
    """Build the series per (pipeline, episode key) — the DB-free core (tested)."""
    parsed: List[Tuple[str, datetime, Dict[str, object]]] = []
    for pipeline_id, envelope in rows:
        env = envelope if isinstance(envelope, dict) else json.loads(envelope)
        parsed.append((pipeline_id, _parse_dt(env['timestamp']), env))
    parsed.sort(key=lambda item: (item[0], item[1]))

    wanted = symbol_filter.upper()
    engines: Dict[str, BreakingEpisodeRule] = {}
    built: Dict[Tuple[str, str], SymbolTimeline] = {}
    previous: Dict[Tuple[str, str], bool] = {}

    for pipeline_id, ts, env in parsed:
        rule = engines.setdefault(pipeline_id, rules.get(pipeline_id) or BreakingEpisodeRule())
        exit_threshold = rule.get_exit_threshold()
        # One sample per (episode key, envelope), not per result: a fanned pair is one analysis, so
        # counting both legs would double every pass, every flip and every mechanical hold.
        pass_state: Dict[str, str] = {}
        pass_breaking: Dict[str, bool] = {}
        pass_scored: Dict[str, bool] = {}
        opened: Dict[str, int] = {}

        for result in env.get('result', []):
            name = result['symbol']
            # The episode key IS the rule's key — the row therefore shows exactly what the rule
            # reasons about. Where that grouping is too coarse (a same-base, different-query FX
            # group), this makes it visible as one row rather than hiding it behind a zero.
            group_key = result.get('base_currency') or name
            key = (pipeline_id, group_key)
            row = built.setdefault(key, SymbolTimeline(pipeline_id))
            if name not in row.symbols:
                row.symbols.append(name)

            is_breaking = bool(result.get('is_breaking'))
            urgency = float(result.get('urgency') or 0.0)
            # Driven for EVERY result, mechanical ones included, exactly as the live tracker drives
            # it. Such a row carries urgency 0.0, so it qualifies for nothing and the rule's state
            # is unchanged either way — but "provably a no-op" is a property of today's rule, and
            # the two paths must not depend on it staying true.
            decision = rule.observe(group_key, ts, is_breaking, urgency)
            if decision.opened:
                opened[group_key] = opened.get(group_key, 0) + 1

            # A mechanically-scored row (retrieval empty after the floor) never reached the model,
            # so it is not evidence about its stability — it gets its own cell rather than being
            # dropped. Dropping it rendered a calm symbol as `~`, i.e. as an outage.
            if result.get('basis') not in (None, 'llm'):
                cell = _CELL_MECHANICAL
            else:
                pass_scored[group_key] = True
                cell = (_CELL_BREAKING if is_breaking
                        else _CELL_HELD if urgency >= exit_threshold
                        else _CELL_BELOW)
                pass_breaking[group_key] = pass_breaking.get(group_key, False) or is_breaking
            if _PRECEDENCE[cell] > _PRECEDENCE.get(pass_state.get(group_key, _CELL_NO_PASS), 0):
                pass_state[group_key] = cell

        for group_key, cell in pass_state.items():
            key = (pipeline_id, group_key)
            row = built[key]
            row.samples.append((ts, cell))
            row.episodes += opened.get(group_key, 0)
            # `passes` and `mechanical` are both per analysis unit per pass, so they sum to the
            # envelope count on every row — merged or not. Counting `mechanical` per result made
            # the two columns different units and the sum meaningless for a merged row.
            if not pass_scored.get(group_key):
                row.mechanical += 1
                continue
            row.passes += 1
            breaking = pass_breaking.get(group_key, False)
            if breaking:
                row.breaking_passes += 1
                row.first_breaking = row.first_breaking or ts
                row.last_breaking = ts
            # A flip is a change of the *verdict*, which is what the episode count reacts to — not
            # a change of urgency (the model wobbles inside a band all the time). Only scored
            # passes can flip it; an unscored one carries the previous verdict forward.
            was = previous.get(key)
            if was is not None and was != breaking:
                row.flips += 1
            previous[key] = breaking

    rows_out = list(built.values())
    if wanted:
        rows_out = [row for row in rows_out if wanted in row.symbols]
    # Breaking units first (most flips first — the noisiest is the one worth reading), then the
    # quiet ones alphabetically. They are kept rather than filtered: "this symbol never broke" and
    # "this symbol was never scored" are both answers, and an absent row is neither.
    rows_out.sort(key=lambda row: (row.pipeline_id, row.breaking_passes == 0,
                                   -row.flips, row.label()))
    stamps = [ts for row in rows_out for ts, _ in row.samples]
    return BreakingTimelineReport(since_label, rows_out, symbol_filter,
                                  since=since or (min(stamps) if stamps else None),
                                  until=until or (max(stamps) if stamps else None))


def _fmt_span(row: SymbolTimeline) -> str:
    if row.first_breaking is None or row.last_breaking is None:
        return '—'
    return (f'{row.first_breaking.strftime("%m-%d %H:%M")} → '
            f'{row.last_breaking.strftime("%m-%d %H:%M")}')


def _strip(row: SymbolTimeline, since: datetime, span: timedelta, cells: int) -> str:
    """Bucket the samples across the window into exactly `cells` characters.

    Bucketing rather than truncating is the point: a 7-day window at ~50 columns is ~3.4h per cell,
    and the alternative — keeping the newest N passes — showed nine hours under a "last 7d" header.
    An empty bucket renders `~`, so an outage reads as absence rather than as calm.
    """
    buckets = [_CELL_NO_PASS] * cells
    total = span.total_seconds()
    for ts, cell in row.samples:
        offset = (ts - since).total_seconds()
        index = min(cells - 1, max(0, int(offset / total * cells))) if total > 0 else cells - 1
        if _PRECEDENCE[cell] > _PRECEDENCE[buckets[index]]:
            buckets[index] = cell
    return ''.join(buckets)


def _resolution(report: BreakingTimelineReport, span: timedelta) -> int:
    """How many cells the data itself supports: `span / cadence`, cadence = the median pass gap."""
    stamps = sorted({ts for row in report.rows for ts, _ in row.samples})
    if len(stamps) < 2:
        return max(1, len(stamps))
    gaps = sorted((later - earlier).total_seconds()
                  for earlier, later in zip(stamps, stamps[1:]))
    cadence = gaps[len(gaps) // 2] or 1.0
    return max(1, int(span.total_seconds() / cadence) + 1)


def format_breaking_timeline_report(report: BreakingTimelineReport, *,
                                    width: Optional[int] = None) -> str:
    """Render as the shared console pattern: title, window line, `----` dividers, aligned columns."""
    term_width = width if width is not None else shutil.get_terminal_size((80, 24)).columns
    title = 'Breaking state timeline'
    if report.symbol_filter:
        title += f' — {report.symbol_filter}'
    label_width = max([9] + [len(row.label()) for row in report.rows])
    header = (f'{"unit":{label_width}} {"passes":>6} {"mech":>5} {"brk":>4} {"flips":>5} '
              f'{"epi":>4}  {"first → last breaking":25}  series')

    fixed = label_width + 1 + 6 + 1 + 5 + 1 + 4 + 1 + 5 + 1 + 4 + 2 + 25 + 2
    available = max(12, term_width - fixed - 1)
    since, until = report.since, report.until
    span = (until - since) if since and until and until > since else timedelta(seconds=1)
    # A cell must never be narrower than the eval cadence, or the console's spare columns would
    # scatter the passes and render the space between them as `~` — an outage that never happened.
    # The cadence is the MEDIAN gap between passes, so a real outage cannot inflate it.
    cells = min(available, _resolution(report, span))

    body: List[str] = []
    current_pipeline = ''
    for row in report.rows:
        if row.pipeline_id != current_pipeline:
            body.append(row.pipeline_id)           # section header per pipeline
            current_pipeline = row.pipeline_id
        strip = _strip(row, since, span, cells) if since else _CELL_NO_PASS * cells
        body.append(f'{row.label():{label_width}} {row.passes:>6} {row.mechanical:>5} '
                    f'{row.breaking_passes:>4} {row.flips:>5} {row.episodes:>4}  '
                    f'{_fmt_span(row):25}  {strip}')

    divider = '-' * min(term_width, max([len(header)] + [len(line) for line in body]))
    window = f'window: last {report.since_label}'
    if since and until:
        window += f'   {since.strftime("%m-%d %H:%M")} → {until.strftime("%m-%d %H:%M")} UTC'
    lines = [
        title,
        window,
        f'{_CELL_BREAKING} breaking · {_CELL_HELD} hold band · {_CELL_BELOW} below · '
        f'{_CELL_MECHANICAL} not scored · {_CELL_NO_PASS} no pass',
        divider,
        header,
        divider,
    ]
    if not body:
        return '\n'.join(lines + ['(no passes in the window)', divider])
    lines.extend(body)
    lines.append(divider)
    breaking_rows = [row for row in report.rows if row.breaking_passes]
    total_flips = sum(row.flips for row in report.rows)
    total_episodes = sum(row.episodes for row in report.rows)
    # The two numbers side by side are the point: flips are the model's noise (unchanged by any
    # grouping rule), episodes are what the rule made of them. Counted over analysis units, so a
    # fanned pair contributes once.
    lines.append(f'{total_flips} verdict flips → {total_episodes} episodes '
                 f'across {len(breaking_rows)} of {len(report.rows)} analysis unit(s)')
    return '\n'.join(lines)
