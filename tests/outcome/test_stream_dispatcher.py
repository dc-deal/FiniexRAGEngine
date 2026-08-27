"""The single journal tailer (ISSUE_9 §3.4) — needs a reachable Postgres (skipped otherwise).

What is asserted here is the guarantee the whole transport rests on: **wire order equals `seq`
order**, so a gap means exactly one thing to a consumer and they need no grace period before calling
it final. Everything else in this file is a corollary — the subscribe race, the drop policy, and the
sweep that makes a lost notification cost a delay instead of a stall.

`asyncio.run` per scenario, matching `tests/pipeline/test_workers.py`: the suite carries no asyncio
plugin, and a coroutine per test keeps the event loop's lifetime exactly the scenario's.
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

import pytest

from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.types.outcome_types import RunMetadata, SentimentEnvelope, SentimentResult

_CHANNEL = 'test_dispatcher_channel'


def _run(coro):
    return asyncio.run(coro)


async def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait for a condition rather than for a duration — a fixed sleep asserts the machine's speed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError('condition not reached within the timeout')


def _envelope(pipeline_id: str = 'p') -> SentimentEnvelope:
    return SentimentEnvelope(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed', prompt_version='4',
        timestamp=datetime.now(timezone.utc), status='success',
        result=[SentimentResult(symbol='BTCUSD', signal='BUY', sentiment_score=0.4,
                                confidence=0.8, reasoning='bullish')],
        metadata=RunMetadata(model='gpt-4o-mini'))


def _drain(subscription) -> List[int]:
    seqs = []
    while not subscription.queue.empty():
        seqs.append(subscription.queue.get_nowait()['seq'])
    return seqs


@pytest.fixture
def store(clean_db: str) -> OutcomeStore:
    return OutcomeStore(clean_db, notify_channel=_CHANNEL)


@pytest.fixture
def dispatcher(store: OutcomeStore, clean_db: str) -> StreamDispatcher:
    return StreamDispatcher(store, clean_db, notify_channel=_CHANNEL,
                            fallback_poll_seconds=1, subscriber_queue_size=8)


# --- the guarantee ------------------------------------------------------------------------------

def test_wire_order_equals_seq_order(store, dispatcher):
    """The property a consumer's immediate-loss verdict depends on. Per-pass enqueueing could not
    promise it: two passes committing milliseconds apart can enqueue in reverse order."""
    async def scenario():
        subscription = await dispatcher.subscribe('p')
        for _ in range(5):
            store.save(_envelope())
        await dispatcher._advance('p')
        return _drain(subscription)

    assert _run(scenario()) == [1, 2, 3, 4, 5]


def test_the_cursor_is_seeded_at_the_head_so_history_is_not_re_delivered(store, dispatcher):
    """The connect path serves history from the store; the fan-out must not serve it again."""
    async def scenario():
        for _ in range(3):
            store.save(_envelope())
        subscription = await dispatcher.subscribe('p')       # attaches at seq 3
        await dispatcher._advance('p')
        before = _drain(subscription)
        store.save(_envelope())
        await dispatcher._advance('p')
        return before, _drain(subscription)

    before, after = _run(scenario())
    assert before == []                                      # nothing back-filled
    assert after == [4]                                      # only what came later


def test_a_pass_committing_during_connect_is_delivered_exactly_once(store, dispatcher):
    """RC-1, played out in the order the router uses it: register, buffer, snapshot, then discard
    from the buffer everything the snapshot already carried."""
    async def scenario():
        store.save(_envelope())                              # seq 1 — before the connect
        subscription = await dispatcher.subscribe('p')       # register FIRST
        store.save(_envelope())                              # seq 2 — commits during connect
        snapshot = store.envelopes_by_seq('p', after_seq=0, limit=10)
        last_replayed = snapshot[-1]['seq']
        await dispatcher._advance('p')
        buffered = _drain(subscription)
        return last_replayed, [seq for seq in buffered if seq > last_replayed]

    last_replayed, live = _run(scenario())
    assert last_replayed == 2                                # the snapshot saw the racing pass
    assert live == []                                        # so the buffered copy is discarded


def test_a_missed_notification_is_healed_by_the_sweep(store, dispatcher):
    """The fallback exists so a notification lost with a dropped connection costs a delay rather
    than a stalled stream — asserted by sweeping without any notification at all."""
    async def scenario():
        subscription = await dispatcher.subscribe('p')
        store.save(_envelope())
        await dispatcher._sweep()
        return _drain(subscription)

    assert _run(scenario()) == [1]


# --- backpressure ------------------------------------------------------------------------------

def test_a_full_queue_drops_that_subscriber_and_nobody_else(store, dispatcher):
    """RC-6: a slow reader is dropped, never accommodated — and the drop is *its own*.

    The slow subscriber is put at its bound before the advance rather than by racing it, because the
    fan-out is synchronous: with both readers merely passive, both would overflow and the test would
    assert nothing about isolation, which is the half that matters. One reader at its limit, one with
    room, two frames — only the first can be dropped.
    """
    async def scenario():
        slow = await dispatcher.subscribe('p')
        fast = await dispatcher.subscribe('p')
        for _ in range(slow.queue.maxsize):                  # at the bound, nothing read yet
            slow.queue.put_nowait({'seq': -1})
        for _ in range(2):
            store.save(_envelope())
        await dispatcher._advance('p')
        return slow.dropped, fast.dropped, dispatcher.subscriber_count('p'), _drain(fast)

    slow_dropped, fast_dropped, live, fast_seqs = _run(scenario())
    assert slow_dropped is True                              # the one that could not keep up
    assert fast_dropped is False                             # and only that one
    assert live == 1                                         # the healthy connection stays attached
    assert fast_seqs == [1, 2]                               # and misses nothing


def test_a_reader_that_keeps_up_is_never_dropped(store, dispatcher):
    async def scenario():
        subscription = await dispatcher.subscribe('p')
        received = []
        for _ in range(20):                                  # far beyond the queue bound
            store.save(_envelope())
            await dispatcher._advance('p')
            received.extend(_drain(subscription))
        return subscription.dropped, received

    dropped, received = _run(scenario())
    assert dropped is False
    assert received == list(range(1, 21))                    # gapless, in order


# --- isolation ----------------------------------------------------------------------------------

def test_one_subscription_never_receives_another_stream(store, dispatcher):
    """One subscription is one contiguous series: interleaving two pipelines would make every other
    `seq` look missing, which is indistinguishable from loss."""
    async def scenario():
        mine = await dispatcher.subscribe('p')
        await dispatcher.subscribe('other')
        store.save(_envelope('p'))
        store.save(_envelope('other'))
        store.save(_envelope('p'))
        await dispatcher._advance('p')
        await dispatcher._advance('other')
        return _drain(mine)

    assert _run(scenario()) == [1, 2]                        # p's own per-stream counter


def test_the_head_is_tracked_so_a_heartbeat_costs_no_query(store, dispatcher):
    """A keep-alive fires every 20 s on every connection; reading the store for each one would make
    an idle stream the most database-expensive thing in the process."""
    async def scenario():
        await dispatcher.subscribe('p')
        store.save(_envelope())
        await dispatcher._advance('p')
        return dispatcher.head('p')

    head = _run(scenario())
    assert head.seq == 1 and head.epoch == 1
    assert head.available_msc is not None


# --- the notification path, end to end ----------------------------------------------------------

def test_a_committed_envelope_reaches_a_subscriber_over_listen_notify(store, dispatcher):
    """The real path: COMMIT -> notification -> advance -> fan-out, with the loop running.

    Kept as one integration case rather than several, because what it proves is only that the wiring
    is connected — every ordering and bounding rule above is asserted without a listener.
    """
    async def scenario():
        task = asyncio.create_task(dispatcher.run())
        try:
            subscription = await dispatcher.subscribe('p')
            await asyncio.to_thread(store.save, _envelope())
            frame = await asyncio.wait_for(subscription.queue.get(), timeout=10)
            return frame
        finally:
            await dispatcher.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    frame = _run(scenario())
    assert frame['seq'] == 1
    assert frame['pipeline_id'] == 'p'
    assert frame['result'][0]['reasoning'] == 'bullish'       # the stored envelope, verbatim
