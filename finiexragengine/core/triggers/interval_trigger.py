"""Interval-pull trigger — runs the pipeline every N seconds."""
import asyncio

from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger, RunCallback
from finiexragengine.types.trigger_types import TriggerReason


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
        # The immediate first run is a *boot* pass, not a scheduled one (ISSUE_87): it happens
        # because the process started, not because a tick was due. It wins even when the start
        # coincides with a boundary — the reason names why the pass ran *now*.
        reason: TriggerReason = 'boot'
        while not self._stopped.is_set():
            await run(reason)
            reason = 'scheduled'
            # Sleep OR stop, whichever comes first — a stop during the wait exits
            # promptly instead of blocking shutdown for up to a full interval.
            try:
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        self._stopped.set()
