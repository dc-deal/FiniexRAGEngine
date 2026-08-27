"""One connection's frame sequence (ISSUE_9 §3) — needs a reachable Postgres (skipped otherwise).

Driven against the async generator rather than through an HTTP client, and that is a requirement
rather than a preference: an SSE stream never ends on its own, so a test reading it over HTTP cannot
close the connection until the server's generator finishes — which deadlocks. `aclose()` on a
generator is clean, so the sequence rules are asserted here and the router's own tests cover only
the requests that terminate by themselves.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.core.outcome.stream_session import StreamSession
from finiexragengine.types.config_types.app_config_types import StreamConfig
from finiexragengine.types.outcome_types import (
    RunError,
    RunMetadata,
    SentimentEnvelope,
    SentimentResult,
)

_CHANNEL = 'test_stream_session'


def _run(coro):
    return asyncio.run(coro)


def _envelope(pipeline_id: str = 'p', status: str = 'success') -> SentimentEnvelope:
    """`reasoning` carries a newline on purpose — model-written text does."""
    now = datetime.now(timezone.utc)
    return SentimentEnvelope(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed', prompt_version='4',
        timestamp=now, status=status,
        result=[] if status == 'error' else [SentimentResult(
            symbol='BTCUSD', signal='BUY', sentiment_score=0.4, confidence=0.8,
            reasoning='bullish\nacross the board')],
        metadata=RunMetadata(model='gpt-4o-mini'),
        errors=[RunError(type='LLM_TIMEOUT', message='too slow', timestamp=now)]
        if status == 'error' else [])


@pytest.fixture
def store(clean_db: str) -> OutcomeStore:
    return OutcomeStore(clean_db, notify_channel=_CHANNEL)


@pytest.fixture
def session(store: OutcomeStore, clean_db: str) -> StreamSession:
    config = StreamConfig(notify_channel=_CHANNEL, heartbeat_seconds=1)
    dispatcher = StreamDispatcher(store, clean_db, notify_channel=_CHANNEL,
                                 subscriber_queue_size=8)
    return StreamSession(dispatcher, StreamReplay(store, config.replay_window_hours), config)


async def _take(session: StreamSession, count: int, **kwargs) -> List[str]:
    """The first `count` frames, then close the generator — the sequence, not a duration."""
    frames: List[str] = []
    generator = session.frames(**kwargs)
    try:
        async for frame in generator:
            frames.append(frame)
            if len(frames) >= count:
                break
    finally:
        await generator.aclose()
    return frames


def _events(frames: List[str]) -> List[str]:
    return [frame.split('\n', 1)[0][len('event: '):] for frame in frames
            if frame.startswith('event: ')]


def _payloads(frames: List[str]) -> List[Dict[str, Any]]:
    return [json.loads(frame.split('data: ', 1)[1].strip())
            for frame in frames if 'data: ' in frame]


# --- the connect sequence -----------------------------------------------------------------------

def test_a_connect_opens_with_retry_then_the_snapshot_then_live(store, session):
    store.save(_envelope())

    frames = _run(_take(session, 3, pipeline_id='p'))

    assert frames[0] == 'retry: 5000\n\n'   # its own block, as in the sample
    assert _events(frames) == ['signal', 'control']
    assert _payloads(frames)[1] == {'code': 'live', 'stream_epoch': 1, 'head_seq': 1}


def test_every_frame_ends_with_the_blank_line_that_dispatches_it(store, session):
    """Per the SSE specification an event is dispatched on a blank line. Without it a conforming
    client buffers forever — and the published sample hid this by supplying the blank line from its
    own join, which is exactly why the renderer is now shared."""
    store.save(_envelope())

    for frame in _run(_take(session, 3, pipeline_id='p'))[1:]:
        assert frame.endswith('\n\n')


def test_a_cold_stream_has_no_snapshot_and_reports_head_seq_zero(session):
    frames = _run(_take(session, 2, pipeline_id='p'))

    assert _events(frames) == ['control']
    assert _payloads(frames)[0] == {'code': 'live', 'stream_epoch': 0, 'head_seq': 0}


def test_the_pushed_frame_equals_the_stored_envelope(store, session):
    """The parity anchor (§3.2): a projected or re-validated frame is where live and archive drift
    silently, so the frame is the stored JSON rather than a model's rendering of it."""
    store.save(_envelope())
    stored = store.envelopes_by_seq('p', 0, 1)[0]

    pushed = _payloads(_run(_take(session, 2, pipeline_id='p')))[0]

    assert pushed == stored


def test_model_written_newlines_are_escaped_not_emitted(store, session):
    store.save(_envelope())

    frame = _run(_take(session, 2, pipeline_id='p'))[1]

    assert frame.count('\n') == 3               # the data line, plus the dispatching blank line
    assert _payloads([frame])[0]['result'][0]['reasoning'] == 'bullish\nacross the board'


def test_an_error_envelope_is_a_frame(store, session):
    """It is a frame because its `seq` exists. Withholding it would punch a hole indistinguishable
    from a dropped frame and fire the consumer's recovery for nothing."""
    store.save(_envelope(status='error'))

    payload = _payloads(_run(_take(session, 2, pipeline_id='p')))[0]

    assert payload['status'] == 'error' and payload['errors'][0]['type'] == 'LLM_TIMEOUT'


def test_no_frame_carries_an_id_line(store, session):
    store.save(_envelope())

    for frame in _run(_take(session, 3, pipeline_id='p')):
        assert 'id:' not in frame


# --- the terminal control codes -----------------------------------------------------------------

def test_an_epoch_mismatch_ends_the_sequence_after_one_control_frame(store, session):
    """Terminal on the connect path too, so the consumer's boot bridge has exactly ONE resync path
    and no second handler inside its live loop."""
    store.save(_envelope())

    async def scenario():
        return [frame async for frame in session.frames('p', since=0, epoch=7)]

    frames = _run(scenario())

    assert len(frames) == 2                      # retry + the control frame, then closed
    assert _payloads(frames)[0] == {'code': 'epoch_changed', 'stream_epoch': 1,
                                    'previous_epoch': 7, 'head_seq': 1}


def test_a_cursor_ahead_of_the_head_ends_the_sequence_too(store, session):
    """Same remedy as `epoch_changed`, deliberately different diagnosis: this one means the CONSUMER
    rewound, and an operator needs to be alerted differently."""
    store.save(_envelope())

    async def scenario():
        return [frame async for frame in session.frames('p', since=9001, epoch=1)]

    frames = _run(scenario())

    assert _payloads(frames) == [{'code': 'cursor_ahead', 'stream_epoch': 1,
                                  'requested_since': 9001, 'head_seq': 1}]


# --- live delivery ------------------------------------------------------------------------------

def test_a_frame_committed_after_the_connect_arrives_live(store, session):
    """The hand-off from replay to live delivery.

    The dispatcher is advanced explicitly rather than left to its LISTEN loop: what this asserts is
    the SESSION's live path, and the notification wiring has its own end-to-end case in
    `test_stream_dispatcher.py`. Driving it here too would make one test depend on two mechanisms.
    """
    async def scenario():
        generator = session.frames('p')                    # bare connect: cold, so no snapshot
        opening = [await anext(generator), await anext(generator)]   # retry + control/live
        await asyncio.to_thread(store.save, _envelope())
        await session._dispatcher._advance('p')
        live = await asyncio.wait_for(anext(generator), timeout=10)
        await generator.aclose()
        return opening, live

    opening, live = _run(scenario())
    assert _payloads(opening)[0]['code'] == 'live'
    assert _payloads([live])[0]['seq'] == 1


def test_history_zero_is_the_explicit_live_only_connect(store, session):
    """The third entry point: no snapshot, straight to `live`. A consumer that already holds the
    current state and only wants what comes next asks for this rather than discarding a frame."""
    store.save(_envelope())

    frames = _run(_take(session, 2, pipeline_id='p', history=0))

    assert _events(frames) == ['control']
    assert _payloads(frames)[0] == {'code': 'live', 'stream_epoch': 1, 'head_seq': 1}


def test_a_quiet_cadence_still_produces_a_keep_alive(store, session):
    """Every 20 s on every view in production (1 s here) — otherwise a consumer's watchdog would
    have to exceed a pass interval, and a dead socket would go unnoticed for longer than a pass."""
    store.save(_envelope())

    frames = _run(_take(session, 4, pipeline_id='p'))

    assert _events(frames)[-1] == 'heartbeat'
    beat = _payloads(frames)[-1]
    assert beat['seq'] == 1 and beat['stream_epoch'] == 1
    assert beat['now_msc'] >= beat['available_msc']           # skew measurable, and sane


def test_a_dropped_subscriber_ends_its_own_sequence(store, session):
    """RC-6 seen from the connection: the dispatcher drops the queue, and the send loop is what
    closes the socket. The resulting `seq` gap is visible and recoverable via `?since=`."""
    async def scenario():
        generator = session.frames('p', history=1)
        frames = [await anext(generator), await anext(generator)]
        # Fill this subscriber's queue past its bound, then push one more frame.
        subscription = next(iter(session._dispatcher._subscriptions['p']))
        for _ in range(subscription.queue.maxsize):
            subscription.queue.put_nowait({'seq': -1})
        await asyncio.to_thread(store.save, _envelope())
        await session._dispatcher._advance('p')
        remaining = [frame async for frame in generator]
        return subscription.dropped, remaining

    dropped, remaining = _run(scenario())
    assert dropped is True
    assert remaining == []                                   # the sequence ends, no error frame


def test_the_first_frame_on_a_cold_stream_is_not_a_rewind(store, session):
    """Epoch 0 means "not known yet", never "epoch zero".

    Read as a real epoch it makes the FIRST envelope of a stream look like a series change, and the
    session would close every consumer attached to a newly added pipeline with a spurious
    `epoch_changed` — a resync signal for a series that never moved.
    """
    async def scenario():
        generator = session.frames('p')                    # cold: head epoch is 0
        opening = [await anext(generator), await anext(generator)]
        await asyncio.to_thread(store.save, _envelope())    # the first envelope, epoch 1
        await session._dispatcher._advance('p')
        first = await asyncio.wait_for(anext(generator), timeout=10)
        await generator.aclose()
        return opening, first

    opening, first = _run(scenario())
    assert _payloads(opening)[0] == {'code': 'live', 'stream_epoch': 0, 'head_seq': 0}
    assert _events([first]) == ['signal']                  # a frame, not a control code
    assert _payloads([first])[0]['stream_epoch'] == 1
