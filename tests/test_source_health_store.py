"""Integration tests for SourceHealthStore — counters, flag+quarantine, recovery, event cap.

Skipped when psycopg or a reachable PostgreSQL is missing, so the suite stays green everywhere.
No API budget is touched (the store is pure DB I/O). Runs against the canonical `source_health`
table in the isolated, migration-built test schema (`clean_db`, ISSUE_14).
"""
import psycopg
import pytest

from finiexragengine.core.observability.source_health_store import SourceHealthStore
from finiexragengine.types.config_types.app_config_types import SourceHealthConfig

_TABLE = 'source_health'


@pytest.fixture
def store(clean_db: str) -> SourceHealthStore:
    config = SourceHealthConfig(flag_after_consecutive_failures=3, quarantine_hours=1,
                                recent_events_kept=5)
    return SourceHealthStore(clean_db, config)


def _fail(store, source_id='cryptoslate', error_type='RATE_LIMITED', status=429):
    return store.record_failure(source_id, 'cryptoslate.com', 'crypto_news',
                                error_type=error_type, status=status, message=f'HTTP {status}')


def test_success_creates_and_counts(store, clean_db):
    store.record_success('fxstreet', 'fxstreet.com', 'forex_news')
    store.record_success('fxstreet', 'fxstreet.com', 'forex_news')
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT total_polls, total_success, consecutive_failures, flagged '
                    f'FROM {_TABLE} WHERE source_id = %s', ('fxstreet',))
        assert cur.fetchone() == (2, 2, 0, False)


def test_consecutive_failures_flag_and_quarantine(store):
    assert _fail(store).consecutive_failures == 1
    assert _fail(store).just_flagged is False           # below threshold (3)
    outcome = _fail(store)                                # third consecutive -> crosses threshold
    assert outcome.consecutive_failures == 3
    assert outcome.just_flagged is True
    assert outcome.quarantined_until is not None
    assert store.should_poll('cryptoslate') is False     # quarantined -> skip polling
    assert store.should_poll('anything_else') is True


def test_success_resets_and_recovers(store, clean_db):
    _fail(store); _fail(store); _fail(store)              # flag + quarantine
    assert store.should_poll('cryptoslate') is False
    recovered = store.record_success('cryptoslate', 'cryptoslate.com', 'crypto_news')
    assert recovered is True                              # was flagged -> recovery signalled
    assert store.should_poll('cryptoslate') is True       # quarantine cleared in memory
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT consecutive_failures, flagged, quarantined_until '
                    f'FROM {_TABLE} WHERE source_id = %s', ('cryptoslate',))
        assert cur.fetchone() == (0, False, None)


def test_recent_events_are_capped(store, clean_db):
    for i in range(8):
        _fail(store, status=500 + i, error_type='HTTP_ERROR')
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT recent_events FROM {_TABLE} WHERE source_id = %s', ('cryptoslate',))
        events = cur.fetchone()[0]
    assert len(events) == 5                                # kept = recent_events_kept
    assert events[-1]['status'] == 507                     # newest retained (500+7)


def test_quarantine_survives_a_restart(store, clean_db):
    _fail(store); _fail(store); _fail(store)              # flag + quarantine, persisted
    # A fresh store instance (worker restart) loads the quarantine from the DB.
    reborn = SourceHealthStore(clean_db, store._config)
    assert reborn.should_poll('cryptoslate') is False
