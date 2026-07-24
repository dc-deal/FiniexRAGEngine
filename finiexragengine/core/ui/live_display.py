"""Flicker-free terminal dashboard for a running engine (ISSUE_26).

The read side of the live display: renders `EngineStats` (plus the live `BudgetGuard` state) on
an interval via `rich.Live`, so an unattended `server_cli --workers --live` run answers three
questions at a glance — is it alive · what did it just do · is anything broken / what is it
spending. The layout fills the screen: stage rows on top are *state* (fixed height, one row per
worker), and the activity stream below is *history*, filling the rest of the terminal.

In live mode rich.Live owns stdout exclusively — the console log handler is suppressed
(`configure_logging(live_mode=True)`, ISSUE_26 Slice 0) and uvicorn's own logging is routed to
the file, so nothing else writes to the terminal and frames never tear.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.pipeline.breaking_episode import EPISODE_GAP
from finiexragengine.core.ui.engine_stats import (
    BreakingRecord,
    BreakingSnapshot,
    EngineStats,
    IngestSnapshot,
    LlmSnapshot,
    RetrievalSnapshot,
    SourcesSnapshot,
)
from finiexragengine.utils.windows_console import disable_quickedit

# The BREAKING section reserves this many episode rows (newest first, blank-padded) so the state
# panel stays fixed-height while listing recent episodes one per line (ISSUE_64).
_MAX_EPISODE_ROWS = 3


def _format_age(seconds: float) -> str:
    """Compact relative age: `3s` · `5m` · `2h14m` — the same vocabulary as the worker logs."""
    if seconds < 90:
        return f'{seconds:.0f}s'
    if seconds < 3600:
        return f'{seconds / 60:.0f}m'
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f'{hours}h{minutes:02d}m'


def _last(now: datetime, last: Optional[datetime]) -> Text:
    """The `last <age>` cell — dim when a stage has never run (the blindness test: it ages)."""
    if last is None:
        return Text('idle', style='dim')
    return Text(f'last {_format_age((now - last).total_seconds())}')


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
                 worker_count: int = 0,
                 refresh_seconds: float = 1.0,
                 console: Optional[Console] = None) -> None:
        self._stats = stats
        self._budget_guard = budget_guard
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
        uptime = _format_age((now - self._started_at).total_seconds())
        spend = self._budget_status().get('day_spend_usd', 0.0) if self._budget_guard else 0.0
        return (f'FiniexRAGEngine — up {uptime} — {self._worker_count} workers '
                f'— ${spend:.3f} today')

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
        self._keyed_rows(table, now, 'SOURCES', self._stats.sources(), self._sources_detail)
        self._keyed_rows(table, now, 'INGEST', self._stats.ingest(), self._ingest_detail)
        self._keyed_rows(table, now, 'RETRIEVAL', self._stats.retrieval(), self._retrieval_detail)
        self._keyed_rows(table, now, 'LLM', self._stats.llm(), self._llm_detail)
        # BUDGET + BREAKING are engine-wide (no per-worker id column).
        table.add_row('BUDGET', '', self._budget_last(), self._budget_detail())
        table.add_row('BREAKING', '', _last(now, self._stats.breaking().last),
                      self._breaking_detail(self._stats.breaking()))
        # Up to N per-episode lines under the summary: `SYMBOL SIGNAL` · live/ended + duration · why
        # it broke (ISSUE_64). A fixed row count keeps the panel height exact.
        self._breaking_episode_rows(table, now)
        return table

    def _keyed_rows(self, table: Table, now: datetime, label: str,
                    snapshots: Dict[str, Any], detail: Callable[[Any], Text]) -> None:
        # One row per worker id; the stage label sits on the first row only, the rest indent under
        # it. An empty stage (no workers registered) still gets one idle line so it never vanishes.
        if not snapshots:
            table.add_row(label, '', Text('idle', style='dim'), Text('—', style='dim'))
            return
        first = True
        for key, snapshot in snapshots.items():
            last_cell = (_last(now, snapshot.last) if snapshot is not None
                         else Text('idle', style='dim'))
            table.add_row(label if first else '', key, last_cell, detail(snapshot))
            first = False

    @staticmethod
    def _sources_detail(snapshot: Optional[SourcesSnapshot]) -> Text:
        if snapshot is None:
            return Text('—', style='dim')
        # Healthy collapses to `N/N ok` (exception density); only deviations spend words.
        healthy = not snapshot.deviations
        head = Text(f'{snapshot.ok}/{snapshot.total} ok',
                    style='green' if healthy else 'yellow')
        if snapshot.deviations:
            head.append('    ')
            head.append(' · '.join(snapshot.deviations), style='red')
        return head

    @staticmethod
    def _ingest_detail(snapshot: Optional[IngestSnapshot]) -> Text:
        if snapshot is None:
            return Text('—', style='dim')
        text = Text(f'{snapshot.fetched} fetched · {snapshot.new} new · '
                    f'${snapshot.cost_usd:.6f} · {snapshot.duration_ms:.0f}ms')
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
                table.add_row('', Text(f'{record.symbol} {record.signal}'),
                              self._episode_status(now, record), self._episode_reason(record))
            shown = len(records)
        for _ in range(_MAX_EPISODE_ROWS - shown):
            table.add_row('', '', '', '')

    @staticmethod
    def _episode_status(now: datetime, record: BreakingRecord) -> Text:
        # Live vs ended, edge-triggered on EPISODE_GAP: a pass within the gap still saw it breaking
        # (live → a red dot + how long it has been running); otherwise the episode closed by the gap
        # rule (ended → how long ago it last broke). Matches the store report's grouping.
        since_seen = (now - record.last_seen).total_seconds()
        if since_seen <= EPISODE_GAP.total_seconds():
            running = _format_age((now - record.started).total_seconds())
            return Text(f'● {running}', style='red')
        return Text(f'{_format_age(since_seen)} ago', style='dim')

    @staticmethod
    def _episode_reason(record: BreakingRecord) -> Text:
        # The why (the LLM's reasoning), truncated by the column's ellipsis; dim so the symbol +
        # status read first. Phase 2 (ISSUE_64) swaps in a dedicated breaking_reason field.
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
