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

**The listener is a SYNC connection waited on in a thread, and that is a platform decision rather
than a style one.** The first version used `psycopg.AsyncConnection`, which needs a selector-based
event loop; Windows defaults to `ProactorEventLoop`, so on the deployed host every connect raised
`InterfaceError` and the tail reconnected every two seconds for 22 hours — serving connect and
replay correctly while pushing nothing, with the dev container green throughout because it is Linux.
A sync connection has no such dependency. `notifies(timeout=…, stop_after=1)` returns **on the first
notification** (measured lag 0.0 s) or empty at the deadline, which is the sweep's cue — the same two
outcomes the async form had, with the platform coupling removed instead of configured around.
`tests/contracts/test_stream_dispatcher_is_platform_neutral.py` pins it: no `AsyncConnection` here,
ever, because no test on a Linux runner can otherwise see the difference.

**It runs whenever there is a store, with or without `--workers`.** The stream is a read surface over
the journal, so it serves a journal another process writes — which is also what lets a dev instance
serve the live contract without paying for a single LLM call.
"""
import asyncio
import logging
from datetime import datetime, timezone
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

# The listener's connect is bounded. `socket.setdefaulttimeout` cannot do it — libpq is C-level and
# ignores it — so an un-timeouted connect here would be the ISSUE_73 shape in a new place: one call
# that never returns, holding a loop that nothing else can advance. `asyncio.wait_for` is not a
# substitute: it abandons the await, not the thread.
_CONNECT_TIMEOUT_SECONDS = 10


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
        # What `/v1/health` reports (the consumer asked for it: today the only way to notice a
        # stalled push path is to hold a connection longer than the cadence and count what did not
        # arrive — a fourteen-minute diagnosis for a fact this process knows immediately).
        self._listening = False                          # is the LISTEN connection up right now
        self._listener_error: Optional[str] = None        # why not, when it is down
        self._last_advance: Dict[str, datetime] = {}      # per stream: when a frame last went out
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

    async def producer_head(self, pipeline_id: str) -> StreamHead:
        """The **producer's** position, read from the store — never this object's own cursor.

        This is what a keep-alive reports, and the distinction is the defect it was built out of.
        The cursor moves only when the push path works, so a heartbeat fed from it reports a stalled
        *tail* as a stalled *producer*: nearly the right answer for the wrong reason, and exactly the
        wrong one when the producer is healthy and only the tail is broken — which is what happened
        on 2026-08-28, where the field sat at the boot head for 22 hours while 143 envelopes were
        produced. A liveness signal must not share fate with the mechanism it monitors.

        One indexed point read per keep-alive per connection. That is the cost of the field meaning
        what the contract says it means, and the earlier "a heartbeat costs no query" optimisation is
        precisely what bought the wrong number.
        """
        try:
            return await asyncio.to_thread(self._store.stream_head, pipeline_id)
        except Exception:                          # noqa: BLE001 — see below
            # A keep-alive is a liveness proof for the socket, so it must go out even when the store
            # cannot be read. Reporting the last known position is honest, and `now_msc` still
            # proves this process is alive, which is the half a consumer cannot get anywhere else.
            logger.warning('[STREAM] %s: head unreadable for the keep-alive — reporting last known',
                           pipeline_id)
            return self.head(pipeline_id)

    def head(self, pipeline_id: str) -> StreamHead:
        """This stream's position as the **dispatcher** knows it — how far the push path has got.

        Internal, and deliberately no longer what a keep-alive reports (see `producer_head`): this
        number is exactly the one that stops moving when the tail breaks, which makes it the right
        thing for `/v1/health` to expose and the wrong thing to put on the wire as liveness.
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
                # Recorded as well as logged: a log line is read once, and the condition this
                # produced went unnoticed for 22 hours because nothing outside the log could see it.
                self._listening = False
                self._listener_error = f'{exc.__class__.__name__}: {exc}'
                logger.warning('[STREAM] listener dropped (%s) — reopening in %.0fs',
                               self._listener_error, _RECONNECT_SECONDS)
                await asyncio.sleep(_RECONNECT_SECONDS)

    def _open_listener(self) -> psycopg.Connection:
        """Open the LISTEN connection. Blocking by nature, so only ever called inside a thread.

        `LISTEN` takes a channel NAME rather than a value, so it cannot be parameterised — quoted as
        an identifier instead of interpolated, because the channel comes from configuration.
        """
        conn = psycopg.connect(self._database_url, autocommit=True,
                               connect_timeout=_CONNECT_TIMEOUT_SECONDS)
        conn.execute(sql.SQL('LISTEN {}').format(sql.Identifier(self._channel)))
        return conn

    @staticmethod
    def _wait_for_payloads(conn: psycopg.Connection, seconds: int) -> List[str]:
        """Block until the FIRST notification or the deadline, whichever comes first.

        `stop_after=1` is what keeps this sub-second: without it the generator collects until the
        deadline and returns everything at once, which would silently turn every push into a delay of
        up to `fallback_poll_seconds` — a latency regression no test asserts against. With it, a
        pending notification returns the round immediately (measured lag 0.0 s) and an empty list
        means the deadline passed, which is the sweep's cue.
        """
        return [notify.payload for notify in conn.notifies(timeout=seconds, stop_after=1)]

    async def _listen_once(self) -> None:
        """One life of the listening connection: LISTEN, catch up, then follow notifications."""
        conn = await asyncio.to_thread(self._open_listener)
        self._listening = True
        self._listener_error = None
        try:
            # Catch up first: anything committed while the connection was down has no notification
            # coming, and a subscriber attached in that window would otherwise wait for the sweep.
            await self._sweep()
            while not self._stopped:
                payloads = await asyncio.to_thread(
                    self._wait_for_payloads, conn, self._fallback_poll_seconds)
                if payloads:
                    for payload in payloads:
                        await self._advance(payload)
                    continue
                # The deadline passed with nothing pending — the fallback's turn. A stream whose
                # notifications keep arriving is advanced by them and needs no sweep.
                await self._sweep()
        finally:
            self._listening = False
            await asyncio.to_thread(conn.close)

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
                self._last_advance[pipeline_id] = datetime.now(timezone.utc)
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

    def status(self) -> Dict[str, Any]:
        """What `/v1/health` publishes about the push path (the consumer asked for it).

        Before this, the only way to notice a stalled tail was to hold a connection longer than the
        cadence and count what did not arrive — a fourteen-minute diagnosis for a fact this process
        knows immediately, and the reason a broken tail went unnoticed for 22 hours. The engine
        already says when a *worker* stops (ISSUE_75/97); the push path simply was not covered,
        because it did not exist when that rule was written.

        Reported per stream rather than folded into `stall.stalled`: a dispatcher is not a worker, and
        one field meaning two things is how a monitor ends up reading the wrong one. `pushed_seq` is
        the DISPATCHER's cursor — deliberately the number that stops moving when the tail breaks,
        which is what makes it worth publishing and what made it wrong on the wire.
        """
        return {
            'enabled': True,
            'listening': self._listening,
            'listener_error': self._listener_error,
            'channel': self._channel,
            'streams': [
                {'pipeline_id': pipeline_id,
                 'pushed_seq': head.seq,
                 'subscribers': len(self._subscriptions.get(pipeline_id, ())),
                 'last_advance_at': (self._last_advance[pipeline_id].isoformat()
                                     if pipeline_id in self._last_advance else None)}
                for pipeline_id, head in sorted(self._cursor.items())
            ],
        }

    def subscriber_count(self, pipeline_id: Optional[str] = None) -> int:
        """Live connections, for `/health`-style reporting and for tests."""
        if pipeline_id is not None:
            return len(self._subscriptions.get(pipeline_id, ()))
        return sum(len(subs) for subs in self._subscriptions.values())
