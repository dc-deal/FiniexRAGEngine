"""In-process breaking wake bus — ingest flags, eval workers wake out-of-band (ISSUE_11)."""
import logging
from collections import defaultdict
from typing import Dict, List

import asyncio

logger = logging.getLogger(__name__)


class BreakingSubscription:
    """One eval worker's wake handle on the bus — an asyncio latch + its wake threshold.

    The per-pipeline sensitivity filter (`breaking.min_importance`) lives *here*: the bus only
    latches this subscription when a published tier reaches it, so an eager pipeline (min 2) wakes
    on a MID cluster while a conservative one (min 3) ignores it — over the same shared corpus.
    """

    def __init__(self, min_importance: int) -> None:
        self._min_importance = min_importance
        self._event = asyncio.Event()

    def notify(self, tier: int) -> bool:
        # Latch only if the flagged tier is hot enough for this pipeline. Returns True when this
        # call newly latched (already-latched or below-threshold → False), so the bus can count
        # who it actually woke without reaching into the event.
        if tier >= self._min_importance and not self._event.is_set():
            self._event.set()
            return True
        return False

    async def wait(self) -> None:
        """Block until a qualifying breaking candidate is flagged, then re-arm."""
        await self._event.wait()
        self._event.clear()


class BreakingBus:
    """Fans breaking candidates from the ingest workers to the eval workers (ISSUE_11).

    Single-node, in-process: no queue infra (the corpus is the durable buffer — the bus is only
    the low-latency *nudge*; a missed nudge just means the eval worker waits for its normal
    interval, and the candidate is already persisted). One ingest worker publishes per source-set;
    every eval worker over that set subscribes with its own `min_importance`.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[BreakingSubscription]] = defaultdict(list)

    def subscribe(self, source_set_id: str, min_importance: int) -> BreakingSubscription:
        subscription = BreakingSubscription(min_importance)
        self._subscribers[source_set_id].append(subscription)
        return subscription

    def publish(self, source_set_id: str, max_tier: int) -> int:
        """Wake every eval worker on this set whose threshold the flagged tier reaches.

        Returns how many workers were newly woken (already-latched / below-threshold excluded).
        """
        if max_tier <= 0:
            return 0
        woken = sum(1 for subscription in self._subscribers.get(source_set_id, [])
                    if subscription.notify(max_tier))
        if woken:
            logger.info('[breaking] %s: tier %d → woke %d eval worker(s)',
                        source_set_id, max_tier, woken)
        return woken
