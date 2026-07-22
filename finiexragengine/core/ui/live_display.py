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
from typing import Any, Callable, Dict, Optional

from rich.console import Console, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.ui.engine_stats import (
    BreakingSnapshot,
    EngineStats,
    IngestSnapshot,
    LlmSnapshot,
    RetrievalSnapshot,
    SourcesSnapshot,
)


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
        layout = Layout()
        # The state block is fixed to its row count; the activity panel takes all remaining height,
        # so a taller terminal grows only the log region (ISSUE_26 — enlarge only the log).
        layout.split_column(
            Layout(state, name='state', size=self._state_height()),
            Layout(activity, name='activity', ratio=1),
        )
        return layout

    def _state_height(self) -> int:
        # One row per worker for SOURCES/INGEST (source-sets) and RETRIEVAL/LLM (pipelines), at
        # least one idle row each, plus the BUDGET + BREAKING rows, plus the panel's two borders.
        sets = max(1, len(self._stats.sources()))
        pipelines = max(1, len(self._stats.retrieval()))
        return 2 * sets + 2 * pipelines + 2 + 2

    def _header(self, now: datetime) -> str:
        uptime = _format_age((now - self._started_at).total_seconds())
        spend = self._budget_status().get('day_spend_usd', 0.0) if self._budget_guard else 0.0
        return (f'FiniexRAGEngine — up {uptime} — {self._worker_count} workers '
                f'— ${spend:.3f} today')

    def _stage_rows(self, now: datetime) -> Table:
        # A grid (no borders): stage label + per-worker id + `last` cell + a free detail column.
        table = Table.grid(padding=(0, 2))
        # Every column is no_wrap so each stage row is exactly one line — that is what makes
        # `_state_height` (rows + border) the real panel height and stops a wrapped cell from
        # pushing a later row out of the reserved block. `id` auto-fits the longest worker id.
        table.add_column('stage', style='bold', width=10, no_wrap=True)
        table.add_column('id', no_wrap=True)
        table.add_column('last', width=11, no_wrap=True)
        table.add_column('detail', no_wrap=True, overflow='ellipsis')

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
        arrow = f' → {"/".join(snapshot.signals)}' if snapshot.signals else ''
        return Text(f'{snapshot.tokens} tok · ${snapshot.cost_usd:.6f} · '
                    f'{snapshot.duration_ms:.0f}ms{arrow}')

    @staticmethod
    def _breaking_detail(snapshot: BreakingSnapshot) -> Text:
        base = f'{snapshot.detected} detected · {snapshot.confirmed} confirmed'
        if snapshot.detail:
            base += f' · {snapshot.detail}'
        style = 'red' if snapshot.confirmed else ('yellow' if snapshot.detected else 'dim')
        return Text(base, style=style)

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
        table = Table.grid(padding=(0, 2))
        table.add_column('time', style='dim', width=8, no_wrap=True)
        table.add_column('stage', style='bold', width=8, no_wrap=True)
        # One line per event (crop, don't wrap). Newest first; the activity panel crops to its
        # height, so a taller terminal simply shows more history — no manual row cap needed.
        table.add_column('message', no_wrap=True, overflow='ellipsis')
        for event in reversed(self._stats.events()):
            table.add_row(event.ts.strftime('%H:%M:%S'), event.stage, event.message)
        return table
