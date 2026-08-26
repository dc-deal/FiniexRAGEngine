"""BreakingBus (ISSUE_11) — the per-pipeline min_importance wake filter over a shared corpus."""
import asyncio

import pytest

from finiexragengine.core.pipeline.breaking_bus import BreakingBus


def test_wakes_only_subscribers_at_or_above_the_flagged_tier():
    async def _scenario():
        bus = BreakingBus()
        eager = bus.subscribe('crypto_news', min_importance=2)
        strict = bus.subscribe('crypto_news', min_importance=3)
        woken = bus.publish('crypto_news', 2)                 # a MID cluster
        # The eager pipeline is latched; the strict one is not (its threshold is HIGH).
        await asyncio.wait_for(eager.wait(), timeout=0.5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(strict.wait(), timeout=0.03)
        return woken

    assert asyncio.run(_scenario()) == 1


def test_publish_to_a_different_set_wakes_nobody():
    async def _scenario():
        bus = BreakingBus()
        sub = bus.subscribe('crypto_news', min_importance=1)
        woken = bus.publish('forex_news', 3)                  # different set
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sub.wait(), timeout=0.03)
        return woken

    assert asyncio.run(_scenario()) == 0


def test_wait_re_arms_after_a_wake():
    async def _scenario():
        bus = BreakingBus()
        sub = bus.subscribe('s', min_importance=1)
        bus.publish('s', 1)
        await asyncio.wait_for(sub.wait(), timeout=0.5)       # first wake consumed
        # After consuming, the latch is clear again — a second wait blocks until re-published.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sub.wait(), timeout=0.03)
        bus.publish('s', 1)
        await asyncio.wait_for(sub.wait(), timeout=0.5)       # re-armed, wakes again

    asyncio.run(_scenario())
