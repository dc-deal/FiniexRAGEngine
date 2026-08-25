"""Flicker-free terminal dashboard for a running engine (ISSUE_26).

The read side of the live display: renders `EngineStats` (plus the live `BudgetGuard` state) on
an interval via `rich.Live`, so an unattended `server_cli --workers --live` run answers three
questions at a glance — is it alive · what did it just do · is anything broken / what is it
spending. The layout fills the screen: stage rows on top are *state* (fixed height, one row per
worker), and the activity stream below is *history*, filling the rest of the terminal.

In live mode rich.Live owns stdout exclusively — the console log handler is suppressed
(`configure_logging(live_mode=True)`, ISSUE_26 Slice 0) and uvicorn's own logging is routed to
the file, so nothing else writes to the terminal and frames never tear.

**That suppression is why `_header_warnings()` exists.** A condition an operator must notice cannot
be a log line here: nothing on the console survives live mode. So instance-wide conditions —
over the RSS ceiling, an unnamed journal — become header segments that appear while they hold and
vanish when they stop. **Add the next one there**, as one entry in that list, not as another
`header +=` and not as a row of its own: a row costs layout in every frame, a segment costs nothing
once the condition clears.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.core.observability.stall_watchdog import StallWatchdog
from finiexragengine.core.ui.engine_stats import (
    BreakingRecord,
    BreakingSnapshot,
    EngineStats,
    IngestSnapshot,
    LlmSnapshot,
    RetrievalSnapshot,
    SourcesSnapshot,
)
from finiexragengine.types.worker_types import WorkerState
from finiexragengine.utils.relative_age import format_age
from finiexragengine.utils.windows_console import disable_quickedit

# Prefix for every header warning (see `LiveDisplay._header_warnings`). Named rather than repeated
# inline so a grep for it finds every warning site at once — including the join that renders them.
WARNING_MARK = '⚠'

# The BREAKING section reserves this many episode rows (newest first, blank-padded) so the state
# panel stays fixed-height while listing recent episodes one per line (ISSUE_64).
_MAX_EPISODE_ROWS = 3


def _last(now: datetime, last: Optional[datetime], *, stalled: bool = False) -> Text:
    """The `last <age>` cell — dim when a stage has never run (the blindness test: it ages).

    A stalled worker (ISSUE_75) paints the cell red. This is the cell that read a perfectly
    neutral `last 212h…` for nine days in August 2026 — an ageing number nobody's eye catches,
    because it looks exactly like `last 4m`. Colour is the whole signal here: the column is
    width-11 and no_wrap, so there is no room for a marker glyph without truncating the age.
    """
    if last is None:
        return Text('idle', style='dim')
    text = f'last {format_age((now - last).total_seconds())}'
    return Text(text, style='red bold') if stalled else Text(text)


def _merge_signal_chips(signals: List[Tuple[str, str, str, str]]) -> str:
    """`(symbol, signal, base, group)` → chips, merging symbols of the SAME analysis group (same
    retrieval `query`, ISSUE_70) so a fanned pair reads as one: `ETHUSD:HOLD` + `ETHEUR:HOLD` (both
    query "Ethereum ETH") → `ETH·USD/EUR:HOLD`. Same-base but different-query symbols (`USDJPY` /
    `USDCAD`) are NOT merged — the *group* is the key, not the base. A lone symbol stays
    `SYMBOL:signal`; the quote is the ticker minus its base. First-seen order preserved."""
    groups: List[List[Any]] = []          # each: [base, signal, [quotes], first_symbol]
    index: Dict[Tuple[str, str], int] = {}
    for symbol, signal, base, group in signals:
        quote = symbol[len(base):] if base and symbol.startswith(base) else symbol
        key = (group, signal)
        if group and key in index:
            groups[index[key]][2].append(quote)
        else:
            if group:
                index[key] = len(groups)
            groups.append([base, signal, [quote], symbol])
    chips: List[str] = []
    for base, signal, quotes, first_symbol in groups:
        if base and len(quotes) > 1:
            chips.append(f'{base}·{"/".join(quotes)}:{signal}')   # merged: ETH·USD/EUR:HOLD
        else:
            chips.append(f'{first_symbol}:{signal}')              # lone: BTCUSD:SELL
    return ' · '.join(chips)


class LiveDisplay:
    """Renders `EngineStats` on an interval (ISSUE_26). One class per file — the aggregation is
    `EngineStats` next door. Started and stopped by the API lifespan alongside the workers.
    """

    def __init__(self, stats: EngineStats, *,
                 budget_guard: Optional[BudgetGuard] = None,
                 stall_watchdog: Optional[StallWatchdog] = None,
                 resource_gauge: Optional[ResourceGauge] = None,
                 worker_count: int = 0,
                 states_provider: Optional[Callable[[], List[WorkerState]]] = None,
                 version: str = '',
                 journal_named: bool = True,
                 refresh_seconds: float = 1.0,
                 console: Optional[Console] = None) -> None:
        self._stats = stats
        self._budget_guard = budget_guard
        # Whether this engine's journal has a name in `journal_names` (ISSUE_9). Surfaced here
        # because the boot warning cannot reach a live console: `--live` runs without a console log
        # handler, so an operator watching the dashboard would never learn that a consumer's release
        # certificate taken against this instance will read `unknown`. Defaults to True so every
        # other caller (tests, CLI paths) stays silent rather than warning about a question it was
        # not asked.
        self._journal_named = journal_named
        # The running build, shown in the header. A live console that does not say which version it
        # is showing makes "did the deploy land?" a guess — and this session had to answer exactly
        # that question from commit timestamps. Empty = omit the segment (CLI/test paths).
        self._version = version
        # Optional (ISSUE_75): asked each frame which workers are stalled, so a silent stage turns
        # red instead of ageing quietly. None = no stall rendering (CLI/test paths).
        self._stall_watchdog = stall_watchdog
        # Optional (ISSUE_89): the process gauge, read only to mark a crossed RSS ceiling in the
        # header. Deliberately NOT a permanent figure — a memory number that is fine 99 % of the
        # time costs a row and trains the eye to skip it, which is the same exception-density rule
        # the SOURCES row follows. None = nothing rendered.
        self._resource_gauge = resource_gauge
        # Optional: the supervisor's own state list, read each frame for one thing only — a
        # worker whose task ENDED. That is stronger than the stall next door: a stalled worker
        # may resume on its next tick, a dead one never will, and only a restart brings it back.
        # None = nothing rendered (CLI/test paths).
        self._states_provider = states_provider
        self._worker_count = worker_count
        self._refresh_seconds = refresh_seconds
        self._console = console if console is not None else Console()
        self._started_at = datetime.now(timezone.utc)
        # Set when the lifespan asks the loop to stop; the render loop waits on it between frames.
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Enter the rich.Live context and re-render until stopped (graceful teardown on exit)."""
        # Import here so the module imports cleanly even where rich.Live's terminal probing would
        # misbehave (tests render via `render()` directly, never entering Live).
        from rich.live import Live

        # Harden the Windows console first: clear QuickEdit so a stray click/keypress can't pause
        # our stdout writes and freeze the event loop (ISSUE_26); a no-op off Windows.
        disable_quickedit()

        # screen=True: the dashboard owns the full terminal via the alternate screen buffer, so the
        # layout fills the whole screen (state block on top, activity stream filling the rest) and
        # exit restores the previous terminal cleanly — no leftover/doubled frame (ISSUE_26).
        # auto_refresh OFF: we own the repaint cadence with an explicit refresh each tick, so rich's
        # background thread never races our update() mid-run. The durable record is the file log.
        with Live(self.render(), console=self._console, screen=True,
                  auto_refresh=False) as live:
            while not self._stop.is_set():
                live.update(self.render(), refresh=True)
                # Wake early if stop is signalled; otherwise tick on the refresh interval.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._refresh_seconds)
                except asyncio.TimeoutError:
                    pass
        # Leaving the `with` block stops Live and restores the pre-run terminal (alternate screen).

    async def stop(self) -> None:
        self._stop.set()

    # --- rendering -------------------------------------------------------------------------

    def render(self) -> RenderableType:
        """Full-screen layout: a fixed state panel on top, the activity stream fills the rest. Pure."""
        now = datetime.now(timezone.utc)
        state = Panel(self._stage_rows(now), title=self._header(now), title_align='left',
                      border_style='cyan')
        activity = Panel(self._activity(now), title='activity', title_align='left',
                         border_style='blue')
        # Measure the state panel at the CURRENT width, so a folded (wrapped) LLM signal row on a
        # narrow console makes the panel taller instead of clipping — the activity panel below then
        # takes whatever is left (ISSUE_70). Capped so the activity keeps at least a few lines even
        # if the state wraps a lot (a very narrow terminal).
        measured = len(self._console.render_lines(state, self._console.options, pad=False))
        height = min(measured, max(6, self._console.height - 3))
        layout = Layout()
        layout.split_column(
            Layout(state, name='state', size=height),
            Layout(activity, name='activity', ratio=1),
        )
        return layout

    def _header(self, now: datetime) -> str:
        uptime = format_age((now - self._started_at).total_seconds())
        spend = self._budget_status().get('day_spend_usd', 0.0) if self._budget_guard else 0.0
        version = f' v{self._version}' if self._version else ''
        header = (f'FiniexRAGEngine{version} — up {uptime} — {self._worker_count} workers '
                  f'— ${spend:.3f} today')
        return header + ''.join(f' — {warning}' for warning in self._header_warnings())

    def _header_warnings(self) -> List[str]:
        """Header segments shown only *while* their condition holds, gone the moment it stops.

        One list rather than a chain of `header +=`: each condition stays a single entry, the
        separator lives in exactly one place, and the next condition added cannot get it wrong.
        They belong in the header rather than in rows of their own because each is a property of
        the whole instance, not of a stage — and a segment that disappears costs no layout.
        """
        warnings: List[str] = []
        # Only while over the ceiling, and only when one is configured (default 0 = off).
        if self._resource_gauge is not None and self._resource_gauge.over_ceiling:
            sample = self._resource_gauge.latest()
            if sample is not None:
                warnings.append(f'{WARNING_MARK} rss {sample.rss_mb:.0f} MB')
        # `--live` runs without a console log handler, so the boot warning about this cannot reach
        # an operator watching the dashboard. Without it they would learn that a consumer's release
        # certificate reads `unknown` only when the certificate comes out.
        if not self._journal_named:
            warnings.append(f'{WARNING_MARK} journal unnamed (see diagnostics.md)')
        # A worker whose task ended is the loudest thing this header can carry: everything that
        # worker feeds is frozen, and it stays frozen until the process is restarted. It earns a
        # segment because the log line announcing it cannot reach a live console at all.
        dead = sorted(state.name for state in self._states()
                      if state.stopped_at is not None)
        if dead:
            warnings.append(f'{WARNING_MARK} WORKER DEAD: {", ".join(dead)} — restart needed')
        return warnings

    def _states(self) -> List[WorkerState]:
        """The supervisor's worker states, or nothing when this display was built without them."""
        return self._states_provider() if self._states_provider is not None else []

    def _stage_rows(self, now: datetime) -> Table:
        # A grid (no borders): stage label + per-worker id + `last` cell + a free detail column.
        # The fixed stage/id/last columns stay no_wrap (one line, never collapse); the detail column
        # WORD-WRAPS (no_wrap left off) so a long signal row breaks at ` · ` boundaries onto more
        # lines on a narrow console instead of truncating — chips stay intact, and the panel height
        # is measured from the result (ISSUE_70), so the wrapped rows are shown, never clipped.
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column('stage', style='bold', width=10, no_wrap=True)
        table.add_column('id', width=22, no_wrap=True, overflow='ellipsis')
        table.add_column('last', width=11, no_wrap=True)
        table.add_column('detail', ratio=1)

        # One row per worker (source-set for SOURCES/INGEST, pipeline for RETRIEVAL/LLM), so the
        # concurrent workers never clobber each other's state (ISSUE_26).
        self._keyed_rows(table, now, 'SOURCES', self._stats.sources(),
                         self._sources_detail, 'ingest')
        self._keyed_rows(table, now, 'INGEST', self._stats.ingest(),
                         self._ingest_detail, 'ingest')
        self._keyed_rows(table, now, 'RETRIEVAL', self._stats.retrieval(),
                         self._retrieval_detail, 'eval')
        self._keyed_rows(table, now, 'LLM', self._stats.llm(), self._llm_detail, 'eval')
        # BUDGET + BREAKING are engine-wide (no per-worker id column).
        table.add_row('BUDGET', '', self._budget_last(), self._budget_detail())
        table.add_row('BREAKING', '', _last(now, self._stats.breaking().last),
                      self._breaking_detail(self._stats.breaking()))
        # Up to N per-episode lines under the summary: `SYMBOL SIGNAL` · live/ended + duration · why
        # it broke (ISSUE_64). A fixed row count keeps the panel height exact.
        self._breaking_episode_rows(table, now)
        return table

    def _keyed_rows(self, table: Table, now: datetime, label: str,
                    snapshots: Dict[str, Any], detail: Callable[[Any], Text],
                    worker_prefix: str) -> None:
        # One row per worker id; the stage label sits on the first row only, the rest indent under
        # it. An empty stage (no workers registered) still gets one idle line so it never vanishes.
        if not snapshots:
            table.add_row(label, '', Text('idle', style='dim'), Text('—', style='dim'))
            return
        stalled = self._stalled_workers()
        first = True
        for key, snapshot in snapshots.items():
            # The display keys rows by source-set / pipeline id, the supervisor names workers
            # `ingest:<id>` / `eval:<id>` — `worker_prefix` is that mechanical mapping (ISSUE_75).
            last_cell = (_last(now, snapshot.last, stalled=f'{worker_prefix}:{key}' in stalled)
                         if snapshot is not None else Text('idle', style='dim'))
            table.add_row(label if first else '', key, last_cell, detail(snapshot))
            first = False

    def _stalled_workers(self) -> Set[str]:
        """Worker names the watchdog currently considers stalled — asked, never re-derived, so the
        threshold lives in exactly one place (ISSUE_75). Empty without a watchdog."""
        return self._stall_watchdog.stalled_workers() if self._stall_watchdog is not None else set()

    @staticmethod
    def _sources_detail(snapshot: Optional[SourcesSnapshot]) -> Text:
        if snapshot is None:
            return Text('—', style='dim')
        # Healthy collapses to `N/N ok` (exception density); only deviations spend words.
        healthy = not snapshot.deviations and snapshot.host_backoff_until is None
        head = Text(f'{snapshot.ok}/{snapshot.total} ok',
                    style='green' if healthy else 'yellow')
        # A set-wide connectivity failure replaces the per-feed list rather than joining it
        # (ISSUE_84): naming every blameless feed is the noise the guard exists to remove, and
        # the operator needs to be sent to the host, not to the feeds.
        if snapshot.host_backoff_until is not None:
            left = format_age((snapshot.host_backoff_until
                                - datetime.now(timezone.utc)).total_seconds())
            detail = f' — {snapshot.host_detail}' if snapshot.host_detail else ''
            head.append('    ')
            head.append(f'⚠ host connectivity{detail} — back-off {left}, no quarantine',
                        style='red')
            return head
        if snapshot.deviations:
            head.append('    ')
            head.append(' · '.join(snapshot.deviations), style='red')
        return head

    @staticmethod
    def _ingest_detail(snapshot: Optional[IngestSnapshot]) -> Text:
        if snapshot is None:
            return Text('—', style='dim')
        # Tokens sit next to the cost (ISSUE_79): the dollar figure rounds to zero on a quiet
        # pass, so it alone told the operator nothing about the work done.
        text = Text(f'{snapshot.fetched} fetched · {snapshot.new} new · '
                    f'{snapshot.tokens} tok · '
                    f'${snapshot.cost_usd:.6f} · {snapshot.duration_ms:.0f}ms')
        if snapshot.truncated:
            text.append(f'    {snapshot.truncated} truncated', style='yellow')
        if snapshot.suspended:
            text = Text('suspended (quota) · ', style='yellow') + text
        return text

    @staticmethod
    def _retrieval_detail(snapshot: Optional[RetrievalSnapshot]) -> Text:
        if snapshot is None:
            return Text('—', style='dim')
        return Text(f'{snapshot.retrieved} retrieved · {snapshot.symbols} symbols')

    @staticmethod
    def _llm_detail(snapshot: Optional[LlmSnapshot]) -> Text:
        if snapshot is None:
            return Text('—', style='dim')
        # Spend + the per-symbol signals, with fanned same-base symbols merged into one chip
        # (ETH·USD/EUR:HOLD, ISSUE_70); when grouping shrank the calls below the symbol count, say so
        # (`N sym / M calls`) so the consolidation is visible, not hidden behind row-count parity.
        summary = f'{snapshot.tokens} tok · ${snapshot.cost_usd:.6f} · {snapshot.duration_ms:.0f}ms'
        if snapshot.calls and snapshot.calls < len(snapshot.signals):
            summary += f' · {len(snapshot.signals)} sym / {snapshot.calls} calls'
        chips = _merge_signal_chips(snapshot.signals)
        return Text(summary + (f' → {chips}' if chips else ''))

    @staticmethod
    def _breaking_detail(snapshot: BreakingSnapshot) -> Text:
        base = f'{snapshot.detected} detected · {snapshot.confirmed} confirmed'
        if snapshot.detail:
            base += f' · {snapshot.detail}'
        style = 'red' if snapshot.confirmed else ('yellow' if snapshot.detected else 'dim')
        return Text(base, style=style)

    def _breaking_episode_rows(self, table: Table, now: datetime) -> None:
        # The last few confirmed episodes, newest first, one per line — a glance at *what* broke,
        # whether it is still live, and *why*, without scanning the activity stream (ISSUE_64).
        # Always emits exactly _MAX_EPISODE_ROWS rows (blank-padded) so the panel height is exact.
        records = list(reversed(self._stats.recent_breaking()))[:_MAX_EPISODE_ROWS]
        if not records:
            table.add_row('', Text('episodes', style='dim'), '', Text('none active', style='dim'))
            shown = 1
        else:
            for record in records:
                table.add_row('', self._episode_identity(record),
                              self._episode_status(now, record), self._episode_reason(record))
            shown = len(records)
        for _ in range(_MAX_EPISODE_ROWS - shown):
            table.add_row('', '', '', '')

    @staticmethod
    def _episode_identity(record: BreakingRecord) -> Text:
        """`SYMBOL SIGNAL · HH:MM` — the row's identity, ending in the episode's start (ISSUE_65).

        Not the full `breaking_episode_id`: at ~50 characters it cannot earn its width here, and its
        first two segments are already implied by the row (this worker's pipeline, the symbol shown).
        What is left is the part that actually varies, and it is the correlation handle — an operator
        reading `16:51` off this panel and a consumer reading `...:2026-08-24T16:51:03Z` off the wire
        are pointing at the same episode.

        It is the same clipped start the id carries after a seeded restart, because both come from
        the one rule — so the panel cannot show a start the wire disagrees with.
        """
        return Text.assemble((f'{record.symbol} {record.signal}', ''),
                             (f' · {record.started.strftime("%H:%M")}', 'dim'))

    @staticmethod
    def _episode_status(now: datetime, record: BreakingRecord) -> Text:
        # Live vs ended, edge-triggered on the episode's own gap: a pass within it still held the
        # story open (live → a red dot + how long it has been running); otherwise the episode closed
        # by the gap rule (ended → how long ago it last held). The gap rides on the record because
        # it is per-pipeline config and this deque mixes pipelines (ISSUE_82). Matches the store
        # report's grouping, which drives the same rule.
        since_seen = (now - record.last_seen).total_seconds()
        if since_seen <= record.gap_seconds:
            running = format_age((now - record.started).total_seconds())
            # `≥` on an inherited episode: the boot replay covers a bounded window, so a story that
            # opened before it has its start clipped and the duration is a lower bound (ISSUE_82).
            return Text(f'● {"≥" if record.started_bounded else ""}{running}', style='red')
        return Text(f'{format_age(since_seen)} ago', style='dim')

    @staticmethod
    def _episode_reason(record: BreakingRecord) -> Text:
        # The why, truncated by the column's ellipsis; dim so the symbol + status read first.
        # The record already carries the preferred line: the eval worker resolves
        # `breaking_reason or reasoning` once (ISSUE_64 Phase 2), so no renderer repeats the rule.
        if not record.reason:
            return Text('—', style='dim')
        return Text(record.reason, style='dim')

    def _budget_status(self) -> dict:
        return self._budget_guard.status() if self._budget_guard is not None else {}

    def _budget_last(self) -> Text:
        status = self._budget_status()
        if not status:
            return Text('—', style='dim')
        return Text('suspended', style='red') if status.get('suspended') else Text('ok', style='green')

    def _budget_detail(self) -> Text:
        status = self._budget_status()
        if not status:
            return Text('—', style='dim')
        if status.get('suspended'):
            reason = status.get('reason') or 'paused'
            retry = status.get('retry_at')
            tail = f' · retry {retry}' if retry else ''
            return Text(f'{reason}{tail}', style='yellow')
        return Text('re-probe —', style='dim')

    def _activity(self, now: datetime) -> Table:
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column('time', style='dim', width=8, no_wrap=True)
        table.add_column('stage', style='bold', width=8, no_wrap=True)
        # One line per event (crop, don't wrap). Newest first; the activity panel crops to its
        # height, so a taller terminal simply shows more history — no manual row cap needed.
        # ratio=1 makes a long message shrink itself, not collapse the time/stage columns.
        table.add_column('message', no_wrap=True, overflow='ellipsis', ratio=1)
        for event in reversed(self._stats.events()):
            table.add_row(event.ts.strftime('%H:%M:%S'), event.stage, event.message)
        return table
