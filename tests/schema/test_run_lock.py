"""One writer per journal (ISSUE_126) — the claim that makes a second instance harmless.

Once the engine is a service, a console start alongside it is a matter of time, and two sets of
workers on one journal produce duplicate envelopes and pay twice for them. These cases are the four
properties that make the lock a protection rather than a new failure mode: it refuses and says who
holds it, it frees itself when its holder dies, it does not collide across deployments, and it
reports its own state rather than remembering a boolean.
"""
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
    """A connection dropped by a timeout would leave a protection that protects nothing.

    So `status()` asks. Here the connection is closed underneath it — the same thing an idle-session
    timeout or a firewall does — and the claim is re-asserted rather than reported from memory.
    """
    lock = RunLock(clean_db, _INSTANCE)
    lock.acquire()
    try:
        assert lock.status()['held'] is True

        lock._connection.close()          # exactly what an idle timeout does to this session
        reasserted = lock.status()

        assert reasserted['held'] is True
        assert reasserted['instance_id'] == _INSTANCE
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
