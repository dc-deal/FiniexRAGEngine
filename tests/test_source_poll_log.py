"""Integration tests for SourcePollLog — the diagnostic journal (ISSUE_76).

Skipped when psycopg or a reachable PostgreSQL is missing, so the suite stays green everywhere.
Runs against the canonical `source_poll_log` table in the isolated, migration-built test schema
(`clean_db`, ISSUE_14) — so migration 004 itself is under test, not hand-written DDL.
"""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from finiexragengine.core.observability.source_poll_log import SourcePollLog
from finiexragengine.types.ingest_types import PollSample

_TABLE = 'source_poll_log'


@pytest.fixture
def journal(clean_db: str) -> SourcePollLog:
    return SourcePollLog(clean_db, retention_days=30)


def _ok(source_id: str = 'coindesk', duration_ms: float = 412.0,
        articles: int = 20) -> PollSample:
    return PollSample(source_id, 'crypto_news', 'ok', duration_ms, articles=articles)


def _failed(source_id: str = 'ecb_press', duration_ms: float = 10_000.0,
            error_type: str = 'UNREACHABLE') -> PollSample:
    return PollSample(source_id, 'forex_news', 'failed', duration_ms, error_type=error_type)


def test_a_successful_poll_is_recorded_with_its_duration(journal, clean_db):
    journal.record(_ok(duration_ms=412.0, articles=20))
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT source_id, source_set, outcome, duration_ms, error_type, status, '
                    f'articles FROM {_TABLE}')
        assert cur.fetchone() == ('coindesk', 'crypto_news', 'ok', 412.0, None, None, 20)


def test_a_failed_poll_is_recorded_too_with_its_duration(journal, clean_db):
    """The reason this unit exists.

    `StageTimer` keeps nothing for a stage that raises, so before ISSUE_76 a timed-out fetch —
    the single most interesting poll a feed can produce — left no trace at all. On 2026-08-15
    that is exactly why "was ecb_press slow or dead?" could not be answered.
    """
    journal.record(_failed(duration_ms=10_004.0, error_type='UNREACHABLE'))
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT outcome, duration_ms, error_type, articles FROM {_TABLE}')
        assert cur.fetchone() == ('failed', 10_004.0, 'UNREACHABLE', 0)


def test_prune_drops_only_samples_past_the_window(clean_db):
    journal = SourcePollLog(clean_db, retention_days=7)
    now = datetime.now(timezone.utc)
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        for age_days in (30, 8, 6, 1):
            cur.execute(f'INSERT INTO {_TABLE} (ts, source_id, source_set, outcome, duration_ms) '
                        "VALUES (%s, 'coindesk', 'crypto_news', 'ok', 400)",
                        (now - timedelta(days=age_days),))
        conn.commit()

    assert journal.prune() == 2                      # the 30d and 8d rows
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM {_TABLE}')
        assert cur.fetchone()[0] == 2                # the 6d and 1d rows survive


def test_recording_prunes_once_per_utc_day(journal, clean_db):
    """The prune rides the first record of a new day — one DELETE per worker per day, no cron."""
    journal.record(_ok())
    pruned_on = journal._pruned_on                    # noqa: SLF001 — asserting the bookkeeping
    assert pruned_on == datetime.now(timezone.utc).date()

    # A stale old row plus more records on the SAME day: the prune must not run again, so the old
    # row survives until tomorrow. (Bounded journal, not a per-poll DELETE.)
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'INSERT INTO {_TABLE} (ts, source_id, source_set, outcome, duration_ms) '
                    "VALUES (%s, 'old', 'crypto_news', 'ok', 1)",
                    (datetime.now(timezone.utc) - timedelta(days=99),))
        conn.commit()
    journal.record(_ok())
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {_TABLE} WHERE source_id = 'old'")
        assert cur.fetchone()[0] == 1                 # still there — same day, no second prune


def test_a_broken_journal_never_fails_the_pass(clean_db):
    """Diagnostics must not become a new cause of the outages they exist to explain.

    The opposite of `SourceHealthStore`, which raises: health drives the reach decision, so losing
    it silently would corrupt behaviour. Losing a diagnostic row costs a sample and nothing else.
    """
    journal = SourcePollLog('postgresql://nobody@127.0.0.1:1/nothing', retention_days=30)
    journal.record(_ok())                             # must not raise
    assert journal.prune() == 0
