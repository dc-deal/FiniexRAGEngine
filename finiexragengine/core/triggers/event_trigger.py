"""Interval trigger that also wakes on a breaking candidate — the eval worker's clock (ISSUE_11)."""
import asyncio
from typing import Callable, List, Optional

from finiexragengine.core.pipeline.breaking_bus import BreakingSubscription
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger, RunCallback
from finiexragengine.types.trigger_types import TriggerReason

# How long to wait before the next *scheduled* tick — recomputed on every iteration, so a
# bar-close-aligned wait (ISSUE_timeframe) never drifts over a long uptime. A relative cadence
# is just `lambda: seconds`; the aligned one is `lambda: seconds_until_next_boundary(now, tf)`.
NextWaitSeconds = Callable[[], float]


class EventTrigger(AbstractTrigger):
    """Fires on each scheduled tick AND immediately on a breaking wake (ISSUE_11).

    The event-push sibling of `IntervalTrigger`: the eval worker still runs on its normal cadence
    (the `next_wait_seconds` provider — a fixed interval, or the next bar close for a
    timeframe-aligned worker), but a breaking candidate at/above the pipeline's `min_importance`
    (filtered in the `BreakingSubscription`) jumps the queue so a flash crash is evaluated in
    seconds, not up to a full bar later. Overlap-free by construction — the pass is awaited before
    the next wait, so a wake or a tick during a pass simply drives the *next* run, never a
    concurrent one. A `stop()` during either wait exits promptly instead of blocking shutdown for
    up to a bar. The provider is re-read each cycle: an aligned worker snaps to the grid regardless
    of when it booted or how long the last pass took.
    """

    def __init__(self, next_wait_seconds: NextWaitSeconds,
                 subscription: Optional[BreakingSubscription] = None) -> None:
        self._next_wait_seconds = next_wait_seconds
        self._subscription = subscription
        self._stopped = asyncio.Event()

    async def start(self, run: RunCallback) -> None:
        self._stopped.clear()
        # The immediate first pass is a boot pass (ISSUE_87); afterwards the wait itself reports
        # what ended it — a wake or the scheduled boundary. Nothing is inferred from the clock.
        reason: TriggerReason = 'boot'
        while not self._stopped.is_set():
            await run(reason)
            if self._stopped.is_set():
                break
            reason = await self._wait_next()

    async def _wait_next(self) -> TriggerReason:
        # Race the wait against a breaking wake and the stop signal, whichever comes first.
        # The timeout is recomputed here (not cached) so the aligned grid stays exact.
        timeout = self._next_wait_seconds()
        waiters: List[asyncio.Task] = [asyncio.ensure_future(self._stopped.wait())]
        wake: Optional[asyncio.Task] = None
        if self._subscription is not None:
            wake = asyncio.ensure_future(self._subscription.wait())
            waiters.append(wake)
        try:
            done, pending = await asyncio.wait(
                waiters, timeout=timeout,
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
        # Which of the two ended the wait is exactly the pass's reason (ISSUE_87) — known here for
        # free, and unrecoverable afterwards: an off-grid timestamp cannot tell a wake from a
        # restart, and `is_breaking` is the LLM's later verdict, not this cause.
        return 'breaking' if wake is not None and wake in done else 'scheduled'

    async def stop(self) -> None:
        self._stopped.set()
