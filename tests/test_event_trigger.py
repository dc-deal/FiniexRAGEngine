"""EventTrigger (ISSUE_11) — interval clock that jumps the queue on a breaking wake."""
import asyncio

from finiexragengine.core.pipeline.breaking_bus import BreakingBus
from finiexragengine.core.triggers.event_trigger import EventTrigger


def test_fires_immediately_then_on_interval_without_a_subscription():
    calls = []

    async def _scenario():
        trigger = EventTrigger(interval_seconds=0.01)

        async def tick():
            calls.append(1)
            if len(calls) >= 3:
                await trigger.stop()

        await asyncio.wait_for(trigger.start(tick), timeout=1.0)

    asyncio.run(_scenario())
    assert len(calls) == 3          # immediate first run, then per interval


def test_breaking_wake_fires_before_the_interval_elapses():
    calls = []

    async def _scenario():
        bus = BreakingBus()
        subscription = bus.subscribe('s', min_importance=2)
        trigger = EventTrigger(interval_seconds=60, subscription=subscription)  # would block a minute

        async def tick():
            calls.append(1)
            if len(calls) == 1:
                bus.publish('s', 3)          # HIGH candidate -> should wake at once
            else:
                await trigger.stop()

        # If the wake did not work, this would hang until the 60s interval (timeout catches it).
        await asyncio.wait_for(trigger.start(tick), timeout=1.0)

    asyncio.run(_scenario())
    assert len(calls) == 2          # immediate run + woke on breaking, not after 60s


def test_stop_interrupts_the_wait_promptly():
    async def _scenario():
        bus = BreakingBus()
        subscription = bus.subscribe('s', min_importance=2)
        trigger = EventTrigger(interval_seconds=60, subscription=subscription)

        async def tick():
            pass

        task = asyncio.create_task(trigger.start(tick))
        await asyncio.sleep(0.01)            # first (immediate) run happened
        await trigger.stop()
        await asyncio.wait_for(task, timeout=1.0)   # returns promptly, not after 60s

    asyncio.run(_scenario())
