"""The boot gate over the minted producer identity (ISSUE_9 follow-up).

Covers the three states the guard exists to separate: a migrated schema names its deployment, a
schema whose identity row was removed refuses to boot, and a row in the wrong format refuses too —
because the format is the consumer's parser contract, not an internal detail.

The malformed case has to bypass the table's own CHECK constraint to exist at all, which is the
point of testing it: the constraint protects a write made *here*, while the guard also covers a
schema restored from a dump written before the constraint existed.
"""
import re

import pytest

from finiexragengine.core.schema.identity_guard import verify_instance_identity
from finiexragengine.exceptions.ragengine_errors import ConfigurationError, VectorStoreError

psycopg = pytest.importorskip('psycopg')


def test_a_migrated_schema_names_its_own_deployment(clean_db: str) -> None:
    """Migration 017 mints exactly one well-formed id, and the guard returns it.

    `clean_db` truncates every data table between tests; this one surviving is a property the suite
    depends on — nothing re-mints, so a truncated identity would take every later DB test with it.
    """
    instance_id = verify_instance_identity(clean_db)
    assert re.fullmatch(r'[0-9a-f]{12}', instance_id)
    # Stable: the guard reads, it never mints, so two boots against one schema agree.
    assert verify_instance_identity(clean_db) == instance_id


def test_a_schema_without_an_identity_refuses_to_boot(clean_db: str) -> None:
    """An empty table is the one state that silently produces unattributable envelopes.

    Without the guard the engine keeps running and stamps `instance_id: ''` on everything it writes
    — which a consumer reads as "produced before the field existed", i.e. as old data from an
    unknown producer. A plausible default papering over a failure is the defect, so it is a refusal.
    """
    with psycopg.connect(clean_db) as conn:
        conn.execute('DELETE FROM journal_identity')
        conn.commit()
    try:
        with pytest.raises(ConfigurationError, match='no instance identity'):
            verify_instance_identity(clean_db)
    finally:
        # Re-mint for the rest of the session: this fixture's schema is shared, and the tests that
        # follow assume a deployment that can name itself.
        with psycopg.connect(clean_db) as conn:
            conn.execute("INSERT INTO journal_identity (singleton, instance_id, minted_at) "
                         "SELECT TRUE, left(replace(gen_random_uuid()::text, '-', ''), 12), now() "
                         'ON CONFLICT (singleton) DO NOTHING')
            conn.commit()


def test_the_wire_format_is_enforced_where_it_is_written(clean_db: str) -> None:
    """A full UUID is refused by the table itself — before it can reach an archive.

    12 lowercase hex is what the consumer's loader parses. The cheapest place to catch a hand
    re-mint that pastes a UUID is the write, because the next cheapest is their parse error.
    """
    with psycopg.connect(clean_db) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE journal_identity SET instance_id = 'AB12-not-hex'")
        conn.rollback()


def test_a_malformed_identity_refuses_to_boot(clean_db: str) -> None:
    """And the guard says the same thing a second time, for a row the constraint never saw.

    Not redundant with the CHECK above: a schema restored from a dump taken before migration 017's
    constraint carries whatever it carried. The guard is what the *engine* trusts, so it re-states
    the format rather than assuming the table was built by this version.
    """
    with psycopg.connect(clean_db) as conn:
        conn.execute('ALTER TABLE journal_identity '
                     'DROP CONSTRAINT journal_identity_instance_id_check')
        conn.execute("UPDATE journal_identity SET instance_id = 'NOT-HEX'")
        conn.commit()
    try:
        with pytest.raises(ConfigurationError, match='12 lowercase hex'):
            verify_instance_identity(clean_db)
    finally:
        with psycopg.connect(clean_db) as conn:
            conn.execute("UPDATE journal_identity "
                         "SET instance_id = left(replace(gen_random_uuid()::text, '-', ''), 12)")
            conn.execute("ALTER TABLE journal_identity ADD CONSTRAINT "
                         "journal_identity_instance_id_check CHECK (instance_id ~ '^[0-9a-f]{12}$')")
            conn.commit()


def test_an_unreachable_database_is_reported_as_such() -> None:
    """A transport failure is not a configuration verdict — the boot path distinguishes them."""
    with pytest.raises(VectorStoreError):
        verify_instance_identity('postgresql://nobody@127.0.0.1:1/none?connect_timeout=1')
