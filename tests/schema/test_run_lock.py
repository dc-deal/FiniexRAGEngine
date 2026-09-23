"""One writer per journal (ISSUE_126) — the claim that makes a second instance harmless.

Once the engine is a service, a console start alongside it is a matter of time, and two sets of
workers on one journal produce duplicate envelopes and pay twice for them. These cases are the four
properties that make the lock a protection rather than a new failure mode: it refuses and says who
holds it, it frees itself when its holder dies, it does not collide across deployments, it reports
its own state rather than remembering a boolean — including when the session was killed server-side,
where the client still believes it is connected — and it leaves no open transaction behind while it
waits.
"""
import psycopg
import pytest

from finiexragengine.core.schema.run_lock import RunLock, _key_for
from finiexragengine.exceptions.ragengine_errors import AlreadyRunningError, VectorStoreError

pytest.importorskip('psycopg')

_INSTANCE = '1dcb470e3d17'
_OTHER_INSTANCE = '9c3fa4c80d95'


def test_a_second_worker_process_is_refused_and_told_who_holds_it(clean_db: str) -> None:
    """A pid alone sends an operator nowhere, so the refusal names the holding session."""
    first = RunLock(clean_db, _INSTANCE)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError) as refusal:
            RunLock(clean_db, _INSTANCE).acquire()
        message = str(refusal.value)
        assert 'held by pid' in message
        assert 'finiexragengine-workers' in message
        # And it says what to do instead, because "refused" without a next step is a dead end.
        assert 'without --workers' in message
    finally:
        first.release()


def test_the_claim_dies_with_its_holder_so_there_is_no_stale_lock_rule(clean_db: str) -> None:
    """The whole reason for a session lock rather than a file.

    A file lock needs a liveness heuristic, and the FiniexDataCollector named its trap: a lock
    naming a pid the operating system has since reused refuses every future start, which turns a
    protection into a permanent outage. A session lock cannot reach that state — the database drops
    it when the connection goes, crash or not.
    """
    first = RunLock(clean_db, _INSTANCE)
    first.acquire()
    first.release()

    second = RunLock(clean_db, _INSTANCE)
    second.acquire()                      # no raise = the claim was genuinely free again
    second.release()


def test_two_deployments_on_one_cluster_do_not_collide(clean_db: str) -> None:
    """Advisory locks are per DATABASE, and our test schema lives inside the production one.

    A constant key would therefore have the suite refusing production's own claim — the protection
    firing on exactly the case it is not meant to cover. Keying on the identity migration 017 mints
    per schema is what keeps two deployments independent.
    """
    assert _key_for(_INSTANCE) != _key_for(_OTHER_INSTANCE)

    production = RunLock(clean_db, _INSTANCE)
    test_schema = RunLock(clean_db, _OTHER_INSTANCE)
    production.acquire()
    try:
        test_schema.acquire()             # no raise = a different deployment is unaffected
        test_schema.release()
    finally:
        production.release()


def test_the_lock_reports_its_own_state_rather_than_remembering_it(clean_db: str) -> None:
    """A connection dropped underneath the lock leaves a protection that protects nothing.

    So `status()` asks. Here the client closes its own socket — one of the two ways the session can
    go — and the claim is re-asserted rather than reported from memory. The server-side way, which
    is the one production actually produces, is the case below.
    """
    lock = RunLock(clean_db, _INSTANCE)
    lock.acquire()
    try:
        assert lock.status()['held'] is True

        lock._connection.close()
        lock._checked_at = None           # ask again now, rather than serve the 10 s cache
        reasserted = lock.status()

        assert reasserted['held'] is True
        assert reasserted['instance_id'] == _INSTANCE
    finally:
        lock.release()


def test_a_session_killed_server_side_is_seen_although_the_client_looks_connected(
        clean_db: str) -> None:
    """The case the client-side check cannot reach, and the reason `status()` queries the server.

    psycopg's `connection.closed` mirrors libpq, which only marks a connection BAD after a failed
    I/O operation. A backend terminated server-side — `pg_terminate_backend`, an
    `idle_in_transaction_session_timeout`, a PostgreSQL restart — therefore leaves the client
    believing it is connected for as long as nobody speaks to it. Measured 2026-09-23: the flag
    stayed False while `pg_locks` held nothing, so a check reading that flag reported a claim over
    no protection at all. This asserts the flag lies AND that `status()` does not.
    """
    lock = RunLock(clean_db, _INSTANCE)
    lock.acquire()
    try:
        assert lock.status()['held'] is True
        killed_pid = lock._connection.info.backend_pid

        with psycopg.connect(clean_db) as executioner, executioner.cursor() as cursor:
            cursor.execute('SELECT pg_terminate_backend(%s)', (killed_pid,))

        # The half the old check trusted — still reporting a healthy connection.
        assert lock._connection.closed is False

        lock._checked_at = None           # ask the server rather than serve the cached verdict
        state = lock.status()

        assert state['held'] is True                      # re-acquired: nobody moved in
        assert lock._connection.info.backend_pid != killed_pid
    finally:
        lock.release()


def test_the_claim_does_not_sit_in_an_open_transaction(clean_db: str) -> None:
    """A lock connection is long-lived by design, so what it leaves open matters for weeks.

    Without `autocommit` the first statement opens an implicit transaction that is never closed, and
    the session then sits `idle in transaction` holding `backend_xmin` — which stops VACUUM
    reclaiming dead tuples across the whole database while the engine runs. Measured 2026-09-23 both
    ways; the advisory lock is held either way, so the only thing autocommit costs is the bloat.
    """
    lock = RunLock(clean_db, _INSTANCE)
    lock.acquire()
    try:
        with psycopg.connect(clean_db) as probe, probe.cursor() as cursor:
            cursor.execute("SELECT state, backend_xmin IS NOT NULL FROM pg_stat_activity "
                           'WHERE pid = %s', (lock._connection.info.backend_pid,))
            state, holds_xmin = cursor.fetchone()

        assert state == 'idle', f'the lock session is {state!r}, not idle'
        assert holds_xmin is False, 'the lock session is pinning the vacuum horizon'
    finally:
        lock.release()


def test_a_public_read_cannot_generate_a_query_per_request(clean_db: str) -> None:
    """`/v1/health` is unauthenticated, so `status()` is reachable by anyone who can reach the box.

    Asking the database on every read would turn the one public route into a query generator. The
    check is therefore rate-limited, and the answer carries `checked_at` so the bound is visible
    rather than implied — a stated staleness beats an unmeasured one.
    """
    lock = RunLock(clean_db, _INSTANCE)
    lock.acquire()
    try:
        first = lock.status()
        second = lock.status()

        assert first['checked_at'] is not None
        assert second['checked_at'] == first['checked_at']   # served from the previous check

        lock._checked_at = None
        assert lock.status()['checked_at'] != first['checked_at']
    finally:
        lock.release()


def test_a_reassert_that_finds_someone_else_says_so_instead_of_claiming_health(clean_db: str) -> None:
    """The case worth building for: our session died and another process moved in.

    Reporting `held: True` from memory here is how a screen says "protected" about a state that is
    not. The honest answer is the one that reaches `/v1/health`.
    """
    lock = RunLock(clean_db, _INSTANCE)
    lock.acquire()
    intruder = RunLock(clean_db, _INSTANCE)
    try:
        lock._connection.close()          # our claim is gone; the intruder can now take it
        intruder.acquire()

        lock._checked_at = None           # ask, rather than serve the verdict from before the loss
        state = lock.status()

        assert state['held'] is False
        assert 'already runs the workers' in state['reason']
    finally:
        intruder.release()
        lock.release()


def test_an_unreachable_database_is_a_transport_failure_not_a_refusal() -> None:
    """"Cannot ask" and "was told no" are different, and only the second means stop."""
    with pytest.raises(VectorStoreError):
        RunLock('postgresql://nobody@127.0.0.1:1/none?connect_timeout=1', _INSTANCE).acquire()
