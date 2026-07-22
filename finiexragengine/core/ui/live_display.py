"""Flicker-free terminal dashboard for a running engine (ISSUE_26).

The read side of the live display: renders `EngineStats` (plus the live `BudgetGuard` state) on
an interval via `rich.Live`, so an unattended `server_cli --workers --live` run answers three
questions at a glance — is it alive · what did it just do · is anything broken / what is it
spending. Stage rows on top are *state* (always complete, ~6 lines); the single stage-tagged
activity stream below is *history* (the only region that grows).

In live mode rich.Live owns stdout exclusively — the console log handler is suppressed
(`configure_logging(live_mode=True)`, ISSUE_26 Slice 0) and uvicorn's own logging is routed to
the file, so nothing else writes to the terminal and frames never tear.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
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

# How many activity lines the stream shows at once (the deque holds more for scrollback-in-memory;
# only this many are painted so the panel height stays bounded).
_STREAM_ROWS = 12


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

        # auto_refresh OFF: we own the repaint cadence with an explicit refresh each tick, so
        # rich's background thread never races our update() mid-run. transient=True clears the
        # live region when Live stops, so shutdown leaves nothing behind — this kills the doubled
        # top border seen on exit (ISSUE_26), which was our final repaint plus rich's own exit
        # paint. The durable record is the file log; the panel does not need to persist.
        with Live(self.render(), console=self._console, screen=False,
                  auto_refresh=False, transient=True) as live:
            while not self._stop.is_set():
                live.update(self.render(), refresh=True)
                # Wake early if stop is signalled; otherwise tick on the refresh interval.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._refresh_seconds)
                except asyncio.TimeoutError:
                    pass
        # Leaving the `with` block stops Live and (transient) clears the region — no extra paint.

    async def stop(self) -> None:
        self._stop.set()

    # --- rendering -------------------------------------------------------------------------

    def render(self) -> RenderableType:
        """Build the full panel: header + stage state rows + activity stream. Pure (testable)."""
        now = datetime.now(timezone.utc)
        body = Group(self._stage_rows(now), Rule('activity', style='dim'), self._activity(now))
        return Panel(body, title=self._header(now), title_align='left', border_style='cyan')

    def _header(self, now: datetime) -> str:
        uptime = _format_age((now - self._started_at).total_seconds())
        spend = self._budget_status().get('day_spend_usd', 0.0) if self._budget_guard else 0.0
        return (f'FiniexRAGEngine — up {uptime} — {self._worker_count} workers '
                f'— ${spend:.3f} today')

    def _stage_rows(self, now: datetime) -> Table:
        # A grid (no borders): stage label + per-worker id + `last` cell + a free detail column.
        table = Table.grid(padding=(0, 2))
        table.add_column('stage', style='bold', width=10)
        table.add_column('id', width=22)
        table.add_column('last', width=9)
        table.add_column('detail', overflow='fold')

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
        table.add_column('time', style='dim', width=8)
        table.add_column('stage', style='bold', width=8)
        table.add_column('message', overflow='fold')
        # Newest first, capped at _STREAM_ROWS so the panel height stays bounded.
        for event in reversed(self._recent_events()):
            table.add_row(event.ts.strftime('%H:%M:%S'), event.stage, event.message)
        return table

    def _recent_events(self) -> List:
        return self._stats.events()[-_STREAM_ROWS:]
