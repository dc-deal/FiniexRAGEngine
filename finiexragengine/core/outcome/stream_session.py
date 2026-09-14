"""One connection's frame sequence (ISSUE_9 §3) — everything between connect and close.

Separated from the router deliberately, and not only for tidiness. The router owns what only HTTP
can own: parameters, the grant, the status codes, the socket. **This** owns the sequence — retry
line, replay, `live`, then live frames and keep-alives — which is the part with rules worth
asserting: the RC-1 discard, the terminal control codes, the heartbeat on a quiet cadence, and the
mid-stream epoch change.

The practical consequence is that the sequence is testable without a socket. That is not a
convenience: an SSE stream never ends on its own, so a test driving it through an HTTP client cannot
close the connection without the server's generator finishing first — it deadlocks. An async
generator, by contrast, is closed with `aclose()`. So the rules are asserted directly here and the
router's tests cover only the requests that terminate by themselves.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.outcome.stream_frames import (
    render_control,
    render_heartbeat,
    render_retry,
    render_signal,
)
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.types.config_types.app_config_types import StreamConfig
from finiexragengine.types.stream_types import StreamSubscription

logger = logging.getLogger(__name__)


def _now_msc() -> int:
    """Server time at emission, so a consumer can measure clock skew against `available_msc`.

    A clock difference nobody can measure eventually gets blamed on the wrong system — the same
    reason the archive line declares its time base instead of leaving it to be inferred.
    """
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class StreamSession:
    """Renders one subscriber's whole life as SSE frames."""

    def __init__(self, dispatcher: StreamDispatcher, replay: StreamReplay,
                 config: StreamConfig) -> None:
        self._dispatcher = dispatcher
        self._replay = replay
        self._config = config

    async def frames(self, pipeline_id: str, history: Optional[int] = None,
                     since: Optional[int] = None,
                     epoch: Optional[int] = None) -> AsyncIterator[str]:
        """The connection, from `retry:` to close. Never returns on a healthy live stream.

        **A bare connect gets the snapshot**: `history` defaults to 1 when neither parameter is
        given, which is what the published sample shows and what a consumer's first session needs —
        it has no cursor yet, so without a snapshot it would sit on a silent socket until the next
        pass. `history=0` is the explicit way to ask for live only.
        """
        if history is None and since is None:
            history = 1
        # RC-1: register BEFORE the snapshot is read. A pass committing in between would otherwise
        # reach nobody — the snapshot predates it, and the fan-out does not know this subscriber yet.
        subscription = await self._dispatcher.subscribe(pipeline_id)
        try:
            yield render_retry()
            plan = await asyncio.to_thread(
                self._replay.plan, pipeline_id, history if since is None else None, since, epoch)
            stream_epoch = plan.head.epoch
            if plan.control is not None:
                yield render_control(plan.control.code, stream_epoch, plan.control.fields)
            if plan.terminal:
                # The cursor is unusable; the remedy is a reconnect. A connection that neither
                # replays nor goes live would be a third state with no handler on either side.
                logger.info('[STREAM] %s: closing after %s', pipeline_id, plan.control.code)
                return
            last_seq = 0
            for envelope in plan.envelopes:
                last_seq = int(envelope.get('seq') or last_seq)
                yield render_signal(envelope)
            # Emitted once, so "the replay ended" is never left to be inferred from a pause.
            yield render_control('live', stream_epoch,
                                 {'head_seq': max(last_seq, plan.head.seq)})
            async for frame in self._live(pipeline_id, subscription, last_seq, stream_epoch):
                yield frame
        finally:
            # Runs on a client disconnect too (the generator is closed), so a vanished consumer never
            # leaves a queue behind that the fan-out keeps filling.
            self._dispatcher.unsubscribe(subscription)

    async def _live(self, pipeline_id: str, subscription: StreamSubscription,
                    last_seq: int, stream_epoch: int) -> AsyncIterator[str]:
        """Live frames, with a keep-alive whenever the cadence is quiet.

        The keep-alive is a **named event**, never an SSE comment: comment lines are discarded by
        conforming clients per spec, so a comment cannot carry state — and this one carries `seq`,
        which is how a stalled producer is told from a healthy connection.
        """
        while True:
            if subscription.dropped:
                logger.warning('[STREAM] %s: connection closed — this subscriber fell behind',
                               pipeline_id)
                return
            try:
                envelope = await asyncio.wait_for(subscription.queue.get(),
                                                  timeout=self._config.heartbeat_seconds)
            except asyncio.TimeoutError:
                # The PRODUCER's head, from the store — not the dispatcher's cursor. A keep-alive
                # is documented as the liveness proof ("a stalled seq is a stalled producer"), so it
                # must not be fed by the push path it is supposed to reveal the failure of.
                head = await self._dispatcher.producer_head(pipeline_id)
                yield render_heartbeat(head.epoch or stream_epoch, head.seq, _now_msc(),
                                       available_msc=head.available_msc)
                continue
            seq = int(envelope.get('seq') or 0)
            if seq <= last_seq:
                continue        # the RC-1 discard: buffered during connect, already sent
            frame_epoch = int(envelope.get('stream_epoch') or stream_epoch)
            if not stream_epoch:
                # Epoch 0 means the sequencer had no counter row when this connection opened — "not
                # known yet", never "epoch zero". Adopting the first frame's epoch is the only
                # correct reading: comparing against 0 would announce a rewind to every consumer
                # attached to a stream before its first pass, which is exactly the state a newly
                # added pipeline is in. The replay path guards the same sentinel the same way.
                stream_epoch = frame_epoch
            elif frame_epoch != stream_epoch:
                # A rewind while we were connected. Terminal, like the connect path: after a rewind
                # the dispatcher's own mark points at a series that no longer exists, so its state is
                # suspect too. The consumer's live handler only has to recognise this and reconnect.
                logger.warning('[STREAM] %s: epoch %d -> %d mid-stream — closing',
                               pipeline_id, stream_epoch, frame_epoch)
                yield render_control('epoch_changed', frame_epoch,
                                     {'previous_epoch': stream_epoch, 'head_seq': seq})
                return
            last_seq = seq
            yield render_signal(envelope)
