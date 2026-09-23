"""One writer per journal, claimed before anything with a side effect (ISSUE_126).

Once the engine runs as a service, somebody will also start it in a console — to look at something,
to test a change, after an RDP session. Both processes then drive the same journal, and for us that
is worse than duplicated work: two sets of workers produce **duplicate envelopes** on one stream and
**spend twice** producing them. The consumer would see two readings per tick with different content
and no way to tell which one the series means.

**The shared resource is the journal, so the journal is what is claimed** — not the machine and not
the checkout. Two checkouts pointed at one database collide; one checkout run twice against two
databases does not. That is the FiniexDataCollector's rule for their output directory, one level
over: they lock the directory because that is what their instances share.

**A session-level advisory lock, which is why there is no stale-lock rule here.** It dies with its
connection, so a crashed engine frees it with no timeout, no pid file and no "is this holder still
alive" heuristic. The collector had to build that heuristic for a file lock and named its trap: a
lock naming a *reused* pid refuses every future start, turning a protection into a permanent outage.
A session lock cannot reach that state. The price is one long-lived connection, which is the only
place in this package that holds one.

**Keyed on the deployment, not the database.** `pg_advisory_lock` is per *database*, so a second
schema on the same cluster — the `finiex_test` the suite migrates inside the production database —
would otherwise collide with production and refuse the suite. The second key is derived from the
`instance_id` migration 017 mints per schema, so two deployments never meet and two processes on one
deployment always do.

**And it is checked against the server, never against a remembered boolean.** A session dropped by
an idle timeout, a firewall or a `pg_terminate_backend` releases the lock while the engine runs on,
leaving a protection that reports nothing and protects nothing — the shape this project hunts.

The obvious check does not work, and the reason is worth keeping: psycopg's `connection.closed`
reports libpq's own status, which is set only after a **failed I/O operation**. A session killed
server-side therefore leaves it `False` indefinitely, because nothing has tried to talk to it.
Measured 2026-09-23 — after `pg_terminate_backend` the client still called the connection open while
`pg_locks` held nothing, and `/v1/health` would have answered `held: true` over no protection at all.

So `status()` asks `pg_locks`, on the lock's own connection, whether **this backend** still holds the
key: a dead session raises there, which is the signal that the claim is gone. It asks at most once
every ten seconds and reports `checked_at` alongside the verdict — `/v1/health` is public, a read of
it must not become a free database query, and a bounded staleness that is stated beats an unbounded
one that is assumed.
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

from finiexragengine.exceptions.ragengine_errors import AlreadyRunningError, VectorStoreError

logger = logging.getLogger(__name__)

# The same 'FRAG' namespace the migration runner locks in, so this engine's advisory keys stay
# recognisable in `pg_locks` next to anything else on the cluster.
_LOCK_NAMESPACE = 0x46524147

# What this connection calls itself in `pg_stat_activity`, so the refusal below can name a holder
# an operator recognises rather than a bare pid.
_APPLICATION_NAME = 'finiexragengine-workers'

_CONNECT_TIMEOUT_SECONDS = 5

# How often `status()` may actually ask the database. `/v1/health` is public and unauthenticated, so
# a check on every read would let any caller generate database queries; ten seconds bounds that at
# six per minute however hard the endpoint is polled, and is far below any reaction time that
# matters here. The verdict carries `checked_at`, so the staleness is visible rather than implied.
_RECHECK_INTERVAL_SECONDS = 10.0


def _key_for(instance_id: str) -> int:
    """The deployment's 12-hex identity, narrowed to the int4 an advisory key is.

    The first eight hex characters are 32 bits, and PostgreSQL's key is **signed**, so the top half
    of that range has to wrap rather than overflow. Collision risk between two deployments on one
    cluster is 1 in 4.3 billion, against the certainty of a collision if the key were constant.
    """
    value = int(instance_id[:8], 16)
    return value - 2 ** 32 if value >= 2 ** 31 else value


class RunLock:
    """The worker role's claim on one journal — taken at boot, released at shutdown."""

    def __init__(self, database_url: str, instance_id: str) -> None:
        self._database_url = database_url
        self._instance_id = instance_id
        self._key = _key_for(instance_id)
        self._connection: Optional[psycopg.Connection] = None
        self._held_since: Optional[datetime] = None
        # `/v1/health` is a sync handler, so FastAPI runs it in a threadpool and two readers can
        # reach `status()` at once. Without this they would both find a dead connection and both
        # call `acquire()`: one wins, the other reports `held: false` naming its own process as the
        # holder. The guard also makes the cached verdict below a consistent pair.
        self._guard = threading.Lock()
        self._verdict: Optional[Dict[str, Any]] = None
        self._checked_at: Optional[datetime] = None

    def acquire(self) -> None:
        """Claim the journal, or refuse to run and name who holds it.

        Called **before any side effect** — before the first worker, before any paid call, before the
        API binds and before anything announces itself. The collector's refusal sat after their
        Telegram alert, their scheduler and their status port, so a second instance told the operator
        it had started and only then discovered it was not allowed to; under a manager that restarts
        on exit, that is one phone alert per restart cycle.
        """
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s, %s)',
                               (_LOCK_NAMESPACE, self._key))
                granted = bool(cursor.fetchone()[0])
        except psycopg.Error as exc:
            connection.close()
            raise VectorStoreError(f'cannot claim the journal: {exc}') from exc

        if not granted:
            holder = self._holder(connection)
            connection.close()
            raise AlreadyRunningError(
                f'another process already runs the workers against this journal '
                f'(instance {self._instance_id}){holder}. Two would produce duplicate envelopes on '
                f'one stream and pay twice for them. Stop the other one, or run this process '
                f'without --workers to serve reads only.')

        # Close whatever we held before replacing it. A re-assert reaches here with the previous
        # connection still referenced — dead in the ordinary case, but a leaked live session would
        # keep holding the key this call just re-took.
        if self._connection is not None and self._connection is not connection:
            try:
                self._connection.close()
            except psycopg.Error:
                pass                 # already gone, which is the state we wanted it in
        self._connection = connection
        self._held_since = datetime.now(timezone.utc)
        logger.info('[RUNLOCK] journal %s claimed for the worker role', self._instance_id)

    def status(self) -> Dict[str, Any]:
        """Whether the claim still stands — asked of the database, never remembered.

        A lock whose session quietly died is a protection that has stopped protecting while still
        looking installed. So this does not report a boolean it kept: it queries `pg_locks` for its
        own backend, and where the claim is gone it tries to take the lock again. Re-acquiring
        succeeds when nobody moved in (the honest repair) and fails when somebody did — which is the
        state that has to reach a screen rather than a log.

        The answer carries `checked_at`, the moment the database was last actually asked. Between
        checks the previous verdict is served unchanged for up to `_RECHECK_INTERVAL_SECONDS`, so a
        reader can see how old the statement is instead of assuming it is current.
        """
        with self._guard:
            now = datetime.now(timezone.utc)
            if (self._verdict is not None and self._checked_at is not None
                    and (now - self._checked_at).total_seconds() < _RECHECK_INTERVAL_SECONDS):
                return dict(self._verdict, checked_at=self._checked_at)
            return self._recheck(now)

    def _recheck(self, now: datetime) -> Dict[str, Any]:
        """Ask, then repair if the answer is no. Caller holds `_guard`."""
        if self._holds_key():
            return self._remember({'held': True, 'since': self._held_since,
                                   'instance_id': self._instance_id}, now)
        logger.warning('[RUNLOCK] the claim is no longer held — re-asserting it')
        try:
            self.acquire()
        except (AlreadyRunningError, VectorStoreError) as exc:
            return self._remember({'held': False, 'since': None,
                                   'instance_id': self._instance_id, 'reason': str(exc)}, now)
        return self._remember({'held': True, 'since': self._held_since,
                               'instance_id': self._instance_id}, now)

    def _remember(self, verdict: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        """Keep the verdict and the moment it was taken, and hand out the pair."""
        self._verdict = verdict
        self._checked_at = now
        return dict(verdict, checked_at=now)

    def _holds_key(self) -> bool:
        """Does THIS backend still hold the advisory key, according to the server?

        The question has to be asked over the wire, because the cheap local answer is wrong.
        psycopg's `connection.closed` mirrors libpq's status, which only becomes BAD after a failed
        I/O operation — so a session terminated server-side (`pg_terminate_backend`, an
        `idle_in_transaction_session_timeout`, a PostgreSQL restart) leaves it `False` for as long
        as nobody tries to use it. Measured 2026-09-23: client `closed=False`, `pg_locks` empty.

        Any driver error here IS the answer — the session cannot be reached, so the claim is not
        standing — and `_recheck` turns that into a re-acquisition rather than a verdict.
        """
        if self._connection is None:
            return False
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    'AND classid = %s AND objid = %s AND granted AND pid = pg_backend_pid()',
                    (_LOCK_NAMESPACE, self._key % 2 ** 32))
                return bool(cursor.fetchone()[0])
        except psycopg.Error:
            return False

    def release(self) -> None:
        """Drop the claim at an orderly shutdown. Closing the connection is what releases it."""
        if self._connection is None:
            return
        try:
            self._connection.close()
        except psycopg.Error:
            pass                     # the session is gone, which is exactly what release means here
        self._connection = None
        self._held_since = None
        self._verdict = None                 # nothing to report from memory once the claim is gone
        self._checked_at = None
        logger.info('[RUNLOCK] journal %s released', self._instance_id)

    def _connect(self) -> psycopg.Connection:
        """The one long-lived connection in this package, and it is long-lived on purpose.

        Session scope is the whole mechanism: the lock exists for exactly as long as this connection
        does. `keepalives` so an idle NAT or firewall cannot silently end a session that is doing
        nothing by design — the lock's own liveness depends on staying connected. They are the
        client's probes, though, and they cannot make the *server* notice a dead client, which is
        why `status()` asks rather than trusting the socket.
        """
        try:
            # `autocommit` is not a style choice here. Without it psycopg opens an implicit
            # transaction on the first statement and never closes it, so this connection sits
            # `idle in transaction` for the life of the service — which pins `backend_xmin` and
            # stops VACUUM reclaiming dead tuples anywhere in the database, on a box whose disk is
            # treated as scarce. Measured 2026-09-23: without it `('idle in transaction', xmin
            # held)`, with it `('idle', no xmin)`, and the advisory lock is held either way.
            # `MigrationRunner` already does this on its own advisory-lock connection.
            return psycopg.connect(self._database_url,
                                   autocommit=True,
                                   connect_timeout=_CONNECT_TIMEOUT_SECONDS,
                                   application_name=_APPLICATION_NAME,
                                   keepalives=1, keepalives_idle=30,
                                   keepalives_interval=10, keepalives_count=3)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to claim the journal: {exc}') from exc

    def _holder(self, connection: psycopg.Connection) -> str:
        """Name the process holding the lock — a pid alone sends an operator nowhere."""
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT activity.pid, activity.backend_start, activity.application_name '
                    'FROM pg_locks lock '
                    'JOIN pg_stat_activity activity ON activity.pid = lock.pid '
                    "WHERE lock.locktype = 'advisory' AND lock.classid = %s "
                    'AND lock.objid = %s AND lock.granted',
                    (_LOCK_NAMESPACE, self._key % 2 ** 32))
                row = cursor.fetchone()
        except psycopg.Error:
            return ''                # naming the holder is a courtesy; failing to is not an error
        if row is None:
            return ''
        pid, started, application = row
        since = started.isoformat() if started is not None else 'unknown'
        return f' — held by pid {pid} ({application or "unnamed"}), connected since {since}'
