"""Interval trigger that also wakes on a breaking candidate — the eval worker's clock (ISSUE_11)."""
import asyncio
from typing import List, Optional

from finiexragengine.core.pipeline.breaking_bus import BreakingSubscription
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger, RunCallback


class EventTrigger(AbstractTrigger):
    """Fires every `interval_seconds` AND immediately on a breaking wake (ISSUE_11).

    The event-push sibling of `IntervalTrigger`: the eval worker still runs on its normal cadence,
    but a breaking candidate at/above the pipeline's `min_importance` (filtered in the
    `BreakingSubscription`) jumps the queue so a flash crash is evaluated in seconds, not up to a
    full interval later. Overlap-free by construction — the pass is awaited before the next wait,
    so a wake or a tick during a pass simply drives the *next* run, never a concurrent one. A
    `stop()` during either wait exits promptly instead of blocking shutdown for up to an interval.
    """

    def __init__(self, interval_seconds: float,
                 subscription: Optional[BreakingSubscription] = None) -> None:
        self._interval_seconds = interval_seconds
        self._subscription = subscription
        self._stopped = asyncio.Event()

    async def start(self, run: RunCallback) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            await run()
            if self._stopped.is_set():
                break
            await self._wait_next()

    async def _wait_next(self) -> None:
        # Race the interval against a breaking wake and the stop signal, whichever comes first.
        waiters: List[asyncio.Task] = [asyncio.ensure_future(self._stopped.wait())]
        if self._subscription is not None:
            waiters.append(asyncio.ensure_future(self._subscription.wait()))
        try:
            _done, pending = await asyncio.wait(
                waiters, timeout=self._interval_seconds,
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancel the losers (interval timeout leaves both pending) and drain their
            # cancellations so no task is left dangling.
            for task in waiters:
                task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop(self) -> None:
        self._stopped.set()
