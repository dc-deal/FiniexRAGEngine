"""Interval-pull trigger — runs the pipeline every N seconds."""
import asyncio

from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger, RunCallback


class IntervalTrigger(AbstractTrigger):
    """Fires a run every `interval_seconds` (ISSUE_10 — the workers' clock).

    Overlap-free by construction: the loop awaits the pass before sleeping, so a slow
    pass delays the next tick instead of stacking a second one. The first run fires
    immediately (a fresh worker should not sit idle for a full interval). `stop()`
    cancels the sleep and returns after the current pass finishes — never mid-pass.
    """

    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = interval_seconds
        self._stopped = asyncio.Event()

    async def start(self, run: RunCallback) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            await run()
            # Sleep OR stop, whichever comes first — a stop during the wait exits
            # promptly instead of blocking shutdown for up to a full interval.
            try:
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        self._stopped.set()
