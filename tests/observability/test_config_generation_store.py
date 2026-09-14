"""Integration tests for ConfigGenerationStore — the activation log (ISSUE_116).

Skipped when psycopg or a reachable PostgreSQL is missing, so the free suite stays green
everywhere. Runs against the canonical `config_generations` table in the isolated, migration-built
test schema (`clean_db`, ISSUE_14) — so migration 015 itself is under test, not hand-written DDL.

What is asserted is the half the registry cannot do: an activation is appended rather than upserted,
and a re-activation is recognised as a **rollback**. That is the case `(new)` is silent for, and the
one that cost a cross-project round trip on 2026-08-27.
"""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from finiexragengine.core.observability.config_generation_store import ConfigGenerationStore
from finiexragengine.types.config_fingerprint_types import ConfigFingerprint

_TABLE = 'config_generations'
_PROCESS = datetime(2026, 9, 14, 8, 26, tzinfo=timezone.utc)


@pytest.fixture
def store(clean_db: str) -> ConfigGenerationStore:
    return ConfigGenerationStore(clean_db)


def _fingerprint(value: str = '3cce880a58d4',
                 pipeline_id: str = 'crypto_sentiment') -> ConfigFingerprint:
    return ConfigFingerprint(value=value, canonical='{}', pipeline_id=pipeline_id,
                             source_set_id='crypto_news')


def _rows(database_url: str):
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT fingerprint, pipeline_id, reason, process_started_at FROM {_TABLE} '
                    'ORDER BY id')
        return cur.fetchall()


def test_an_activation_is_appended_and_a_restart_appends_a_second(store, clean_db):
    """Append-only: the registry upserts one row per configuration, this keeps one per activation.

    Five boots in ten minutes have to stay five readable rows — collapsing them is exactly how the
    timeline stops being able to show a reverted excursion.
    """
    assert store.log_activation(_fingerprint(), process_started_at=_PROCESS) == 'boot'
    assert store.log_activation(_fingerprint(),
                                process_started_at=_PROCESS + timedelta(minutes=2)) == 'boot'

    rows = _rows(clean_db)
    assert [(row[0], row[2]) for row in rows] == [('3cce880a58d4', 'boot'),
                                                  ('3cce880a58d4', 'boot')]
    assert rows[0][3] != rows[1][3]                     # each row names the process that wrote it


def test_a_generation_becoming_current_again_is_recorded_as_a_rollback(store, clean_db):
    """A → B → A, the sequence the registry erases: it would move only A's `last_seen`.

    The third row is the finding. Nothing else in the system can state it — `register()` reports
    False for a known fingerprint, so the boot line of a rollback is byte-identical to a restart's.
    """
    first = _fingerprint('3cce880a58d4')
    second = _fingerprint('9458492ce234')
    assert store.log_activation(first, process_started_at=_PROCESS) == 'boot'
    assert store.log_activation(second, process_started_at=_PROCESS) == 'boot'
    assert store.log_activation(first, process_started_at=_PROCESS) == 'rollback'

    assert [(row[0], row[2]) for row in _rows(clean_db)] == [
        ('3cce880a58d4', 'boot'), ('9458492ce234', 'boot'), ('3cce880a58d4', 'rollback')]


def test_a_generation_new_to_this_stream_is_a_boot_even_after_another_ran(store, clean_db):
    """Only a RE-activation is a rollback; a genuinely new generation has nothing to roll back to."""
    store.log_activation(_fingerprint('3cce880a58d4'), process_started_at=_PROCESS)

    assert store.log_activation(_fingerprint('9458492ce234'),
                                process_started_at=_PROCESS) == 'boot'


def test_streams_are_judged_separately(store, clean_db):
    """Streams activate independently, so one stream's history must not decide another's reason."""
    store.log_activation(_fingerprint('3cce880a58d4', 'crypto_sentiment'),
                         process_started_at=_PROCESS)
    store.log_activation(_fingerprint('9458492ce234', 'crypto_sentiment'),
                         process_started_at=_PROCESS)

    # The same value arriving on a stream that never ran it is that stream's first sighting.
    assert store.log_activation(_fingerprint('3cce880a58d4', 'forex_macro_sentiment'),
                                process_started_at=_PROCESS) == 'boot'


def test_an_explicit_reason_is_written_as_given(store, clean_db):
    """ISSUE_115 writes `reload` itself: the derivation is the default, not a veto."""
    assert store.log_activation(_fingerprint(), process_started_at=_PROCESS,
                                reason='reload') == 'reload'
    assert [row[2] for row in _rows(clean_db)] == ['reload']


def test_a_failed_write_is_swallowed_and_never_claims_a_rollback(clean_db):
    """A boot must not die because its provenance write did — and must not invent a finding either.

    The registry makes the same trade for the same reason: losing a row costs an explanation, never
    a signal. Reporting `boot` on an unknown answer is the conservative half — a swallowed error
    that reported `rollback` would put a fabricated event into the log line.
    """
    broken = ConfigGenerationStore(clean_db, table='config_generations_missing')

    assert broken.log_activation(_fingerprint(), process_started_at=_PROCESS) == 'boot'
    assert _rows(clean_db) == []
