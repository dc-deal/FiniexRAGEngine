"""One reader tailing the journal, fanning out to every live connection (ISSUE_9 §3.4).

**Passes only commit; this unit does the delivering.** The obvious alternative — each pass enqueues
its own envelope right after committing — is ordered in the store and *unordered on the wire*: two
passes committing 40 ms apart can enqueue in the reverse order if the first thread is descheduled
between COMMIT and the enqueue. A consumer treating a `seq` gap as immediate loss would then declare
the earlier number lost and drop it when it arrives below their cursor. A single reader walking
forward by `seq` makes wire order equal `seq` order **by construction**, so the consumer needs no
grace period before calling a gap final. That guarantee is the whole reason this file exists.

Two more properties follow from the same shape:

* **the crash window closes on its own.** An envelope committed by a process that died before pushing
  is simply read on the next advance — there is nothing to replay by hand;
* **backpressure is isolated from the engine.** The dispatcher is not a pass thread, so a slow
  consumer cannot delay a pass. That is the ISSUE_73/74 discipline restated for the transport: a
  reader is dropped, never accommodated.

**Woken by LISTEN/NOTIFY, swept on a timer.** PostgreSQL delivers a notification on COMMIT, which is
exactly the semantics needed — the wake-up cannot arrive before the row is readable. The periodic
sweep is the belt to that braces: a notification lost with a dropped connection then delays a frame
by one sweep instead of stalling a stream until someone reconnects.

**It runs whenever there is a store, with or without `--workers`.** The stream is a read surface over
the journal, so it serves a journal another process writes — which is also what lets a dev instance
serve the live contract without paying for a single LLM call.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

import psycopg
from psycopg import sql

from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.types.stream_types import StreamHead, StreamSubscription

logger = logging.getLogger(__name__)

# How long to wait before reopening the listening connection after it failed. Deliberately short and
# fixed: the failure is almost always the database restarting under us, and every second of it is a
# second in which frames are delivered only by the sweep of a connection that no longer exists.
_RECONNECT_SECONDS = 2.0


class StreamDispatcher:
    """Tails `outcomes` by `seq` and fans out to the subscriptions of each stream."""

    def __init__(self, store: OutcomeStore, database_url: str,
                 notify_channel: str = 'finiex_outcomes',
                 fallback_poll_seconds: int = 5,
                 subscriber_queue_size: int = 64,
                 batch_size: int = 100) -> None:
        self._store = store
        self._database_url = database_url
        self._channel = notify_channel
        self._fallback_poll_seconds = fallback_poll_seconds
        self._queue_size = subscriber_queue_size
        # How many envelopes one advance may read. A bound, not a tuning knob: it exists so a stream
        # that fell far behind (a long dispatcher outage) is caught up in several bounded reads
        # rather than one unbounded one — the advance loops until it is current.
        self._batch_size = batch_size
        self._subscriptions: Dict[str, List[StreamSubscription]] = {}
        # The highest `seq` handed to the fan-out per stream, plus the epoch and availability stamp
        # the heartbeat reports. Tracked here so a keep-alive costs no query: on a quiet stream the
        # dispatcher's position *is* the head.
        self._cursor: Dict[str, StreamHead] = {}
        self._stopped = False

    # --- subscription ---------------------------------------------------------------------------

    async def subscribe(self, pipeline_id: str) -> StreamSubscription:
        """Register a connection **before** its snapshot is read — this is RC-1.

        A pass committing between the snapshot read and the registration would otherwise reach
        nobody: the snapshot predates it and the fan-out does not know the subscriber yet. So the
        order is register, buffer, snapshot, then discard from the buffer everything the snapshot
        already carried (`seq <= last replayed`, which the caller applies because only it knows how
        far its replay went). Unimplementable without `seq`.

        A stream nobody is watching is not tailed at all — its cursor is seeded here, at the current
        head, so the fan-out never re-delivers history the connect path is about to serve.
        """
        subscription = StreamSubscription(pipeline_id=pipeline_id,
                                         queue=asyncio.Queue(maxsize=self._queue_size))
        if pipeline_id not in self._cursor:
            self._cursor[pipeline_id] = await asyncio.to_thread(
                self._store.stream_head, pipeline_id)
        self._subscriptions.setdefault(pipeline_id, []).append(subscription)
        logger.info('[STREAM] %s: subscriber attached at seq %d (%d live)', pipeline_id,
                    self._cursor[pipeline_id].seq, len(self._subscriptions[pipeline_id]))
        return subscription

    def unsubscribe(self, subscription: StreamSubscription) -> None:
        """Detach one connection. The stream's cursor stays — a reconnect within the same process
        then continues from it rather than re-reading the head."""
        live = self._subscriptions.get(subscription.pipeline_id)
        if live is None:
            return
        if subscription in live:
            live.remove(subscription)
        if not live:
            del self._subscriptions[subscription.pipeline_id]
        logger.info('[STREAM] %s: subscriber detached (%d live)', subscription.pipeline_id,
                    len(live))

    def head(self, pipeline_id: str) -> StreamHead:
        """This stream's position as the dispatcher knows it — what a heartbeat reports.

        Falls back to the store only for a stream with no subscribers, which is a state a heartbeat
        cannot be in (there is no connection to send one on) and therefore only a caller's
        diagnostic path reaches.
        """
        known = self._cursor.get(pipeline_id)
        if known is not None:
            return known
        return self._store.stream_head(pipeline_id)

    # --- the tail loop --------------------------------------------------------------------------

    async def run(self) -> None:
        """Listen, advance, sweep — until `stop()`. Reconnects on its own; never raises upward.

        A failure here must not take the API down: the stream degrades to nothing being pushed while
        `/latest` and the reports keep answering, which is a strictly better outcome than a boot loop.
        """
        logger.info('[STREAM] dispatcher listening on %s · sweep every %ds',
                    self._channel, self._fallback_poll_seconds)
        while not self._stopped:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:               # noqa: BLE001 — see the docstring
                logger.warning('[STREAM] listener dropped (%s: %s) — reopening in %.0fs',
                               exc.__class__.__name__, exc, _RECONNECT_SECONDS)
                await asyncio.sleep(_RECONNECT_SECONDS)

    async def _listen_once(self) -> None:
        """One life of the listening connection: LISTEN, catch up, then follow notifications."""
        async with await psycopg.AsyncConnection.connect(
                self._database_url, autocommit=True) as conn:
            # `LISTEN` takes a channel NAME, not a value, so it cannot be parameterised — quoted as
            # an identifier instead of interpolated, because the channel comes from configuration.
            await conn.execute(sql.SQL('LISTEN {}').format(sql.Identifier(self._channel)))
            # Catch up first: anything committed while the connection was down has no notification
            # coming, and a subscriber attached in that window would otherwise wait for the sweep.
            await self._sweep()
            while not self._stopped:
                # The generator ends when the timeout elapses, which is the sweep's tick — so one
                # loop serves both the notification path and the fallback without a second task.
                async for notify in conn.notifies(timeout=self._fallback_poll_seconds):
                    await self._advance(notify.payload)
                    if self._stopped:
                        return
                await self._sweep()

    async def _sweep(self) -> None:
        """Advance every watched stream — the path that does not depend on a notification."""
        for pipeline_id in list(self._subscriptions):
            await self._advance(pipeline_id)

    async def _advance(self, pipeline_id: str) -> None:
        """Read forward from this stream's cursor and fan out, in `seq` order, until current.

        Reads run in a thread: the store is blocking psycopg, and holding the event loop over a
        journal read would stall every other connection's heartbeat.
        """
        if pipeline_id not in self._subscriptions:
            return                      # nobody is watching; the cursor is seeded on subscribe
        while not self._stopped:
            cursor = self._cursor[pipeline_id]
            envelopes = await asyncio.to_thread(
                self._store.envelopes_by_seq, pipeline_id, cursor.seq, self._batch_size)
            if not envelopes:
                return
            for envelope in envelopes:
                self._cursor[pipeline_id] = StreamHead(
                    seq=int(envelope.get('seq') or cursor.seq),
                    epoch=int(envelope.get('stream_epoch') or cursor.epoch),
                    available_msc=envelope.get('available_msc'))
                cursor = self._cursor[pipeline_id]
                self._fan_out(pipeline_id, envelope)
            if len(envelopes) < self._batch_size:
                return

    def _fan_out(self, pipeline_id: str, envelope: Dict[str, Any]) -> None:
        """Hand one envelope to every subscriber of this stream; drop the ones that cannot take it."""
        for subscription in list(self._subscriptions.get(pipeline_id, ())):
            try:
                subscription.queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # RC-6. Dropping is the policy, not a failure: the alternative is letting one slow
                # reader delay delivery for everyone, and the gap this leaves is visible in `seq`
                # and recoverable with `?since=`.
                subscription.dropped = True
                self.unsubscribe(subscription)
                logger.warning('[STREAM] %s: subscriber dropped — queue full at %d frames',
                               pipeline_id, subscription.queue.maxsize)

    async def stop(self) -> None:
        """Ask the loop to end. In-flight reads finish; nothing is cancelled mid-query."""
        self._stopped = True

    def subscriber_count(self, pipeline_id: Optional[str] = None) -> int:
        """Live connections, for `/health`-style reporting and for tests."""
        if pipeline_id is not None:
            return len(self._subscriptions.get(pipeline_id, ()))
        return sum(len(subs) for subs in self._subscriptions.values())
