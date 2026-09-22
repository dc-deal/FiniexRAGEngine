"""A console that draws another machine's engine, and says so when it stops hearing from it.

Everything on this screen was measured elsewhere. The instant the connection drops, every number on
it describes the past while looking exactly as current as it did a second earlier — so the viewer's
own condition is not a line inside the panel, it is the **frame around it**.

What that costs, and why each part is there (the FiniexDataCollector's §5, adopted whole):

- **The whole frame turns red**, carrying the clock time of the last good reading and its age. One
  line inside a busy panel is a line the eye skips; a border is not.
- **`never answered` before the first reading**, rather than an invented time.
- **The old numbers stay, marked as old.** Blanking them throws away what the engine last said, which
  is usually the interesting part of an outage.
- **The screen redraws on its own clock, faster than it polls.** The panel's contents are frozen
  between readings — every age in them is measured against the instant the engine stamped — so the
  only thing that may keep moving is the age of the reading itself. If that froze too, a dead feed
  would look like a quiet engine.
- **The two clocks are compared.** Uptime and every age come from the producer's clock; if this
  machine's differs by more than a few seconds, the difference is stated rather than absorbed.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console, RenderableType
from rich.panel import Panel

from finiexragengine.core.ui.dashboard_feed import DashboardFeed, FeedReading
from finiexragengine.core.ui.live_display import LiveDisplay
from finiexragengine.core.ui.remote_engine_state import RemoteEngineState
from finiexragengine.utils.relative_age import format_age

logger = logging.getLogger(__name__)

# Beyond this the two machines' clocks are named on screen rather than quietly carried inside every
# age. Small enough that ordinary NTP drift stays silent, large enough not to flicker.
_SKEW_TOLERANCE_SECONDS = 5.0


class DashboardViewer:
    """Polls one engine and renders its state, framed by this viewer's own condition."""

    def __init__(self, feed: DashboardFeed, *,
                 poll_seconds: float = 5.0,
                 refresh_seconds: float = 1.0,
                 console: Optional[Console] = None) -> None:
        self._feed = feed
        self._poll_seconds = poll_seconds
        # Deliberately faster than the poll: the age of the last reading is the one number that must
        # keep moving while nothing is arriving.
        self._refresh_seconds = min(refresh_seconds, poll_seconds)
        self._console = console if console is not None else Console()
        self._last_good: Optional[RemoteEngineState] = None
        self._last_good_at: Optional[datetime] = None
        self._reason = ''
        self._skew_seconds: Optional[float] = None
        self._stop = asyncio.Event()

    def take(self, reading: FeedReading) -> None:
        """Fold one poll result in. A failure never discards what the engine last said."""
        if reading.ok and reading.state is not None:
            self._last_good = reading.state
            self._last_good_at = reading.at
            self._reason = ''
            stamped = reading.state.snapshot_at()
            self._skew_seconds = ((reading.at - stamped).total_seconds()
                                  if stamped is not None else None)
            return
        self._reason = reading.reason

    def render(self, now: Optional[datetime] = None) -> RenderableType:
        """The panel as the engine last described it, inside this viewer's verdict about itself."""
        now = now if now is not None else datetime.now(timezone.utc)
        if self._last_good is None:
            return Panel('', title=f'never answered — {self._reason or "connecting"}',
                         title_align='left', border_style='red')

        state = self._last_good
        display = LiveDisplay(state,
                              budget_guard=state.budget(),
                              stall_watchdog=state.watchdog(),
                              resource_gauge=state.gauge(),
                              states_provider=state.states_provider(),
                              worker_count=state.worker_count(),
                              version=state.version(),
                              journal_named=state.journal_named(),
                              started_at=state.engine_started_at(),
                              now_provider=state.snapshot_at,
                              console=self._console)
        return Panel(display.render(), title=self._title(now), title_align='left',
                     border_style='red' if self._reason else 'cyan')

    def _title(self, now: datetime) -> str:
        """What this viewer knows about itself: where it read, how long ago, and what went wrong."""
        age = format_age((now - self._last_good_at).total_seconds()) if self._last_good_at else '—'
        if self._reason:
            stamp = self._last_good_at.strftime('%H:%M:%S') if self._last_good_at else 'never'
            return f'no answer since {stamp} UTC ({age} ago) — {self._reason}'
        title = f'{self._feed.url()} · read {age} ago'
        # Only when it matters: uptime and every age on the panel come from the producer's clock, so
        # a viewer running minutes off would print a panel that is internally consistent and wrong
        # about when any of it happened.
        if self._skew_seconds is not None and abs(self._skew_seconds) > _SKEW_TOLERANCE_SECONDS:
            title += f' · clocks differ by {self._skew_seconds:+.0f}s'
        return title

    async def run(self) -> None:
        """Poll on one clock, redraw on a faster one, until stopped."""
        from rich.live import Live

        next_poll = 0.0
        with Live(self.render(), console=self._console, screen=True,
                  refresh_per_second=4, transient=False) as live:
            while not self._stop.is_set():
                if next_poll <= 0.0:
                    self.take(await asyncio.to_thread(self._feed.poll))
                    next_poll = self._poll_seconds
                live.update(self.render(), refresh=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._refresh_seconds)
                except asyncio.TimeoutError:
                    next_poll -= self._refresh_seconds

    async def stop(self) -> None:
        self._stop.set()
