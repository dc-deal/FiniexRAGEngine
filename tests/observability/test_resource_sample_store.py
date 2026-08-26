"""Integration tests for the resource sample store (ISSUE_89).

Runs against the migration-built test schema (`clean_db`, ISSUE_14), so migration 008 is exercised
rather than hand-written DDL. No API budget — pure DB I/O.

The swallow rule is the load-bearing one: the writer is the stall watchdog's tick, and a diagnostic
that can take the watchdog down would be the same irony `source_poll_log` was written to avoid.
"""
from datetime import datetime, timedelta, timezone

import psycopg

from finiexragengine.core.observability.resource_sample_store import ResourceSampleStore
from finiexragengine.types.resource_types import ResourceSample

_TABLE = 'resource_samples'


def _sample(minutes_ago: float = 0.0, rss: float = 412.0, sockets=24, threads=31):
    return ResourceSample(ts=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
                          rss_mb=rss, open_sockets=sockets, threads=threads)


def test_a_sample_round_trips(clean_db):
    store = ResourceSampleStore(clean_db)
    store.record(_sample())
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT rss_mb, open_sockets, threads FROM {_TABLE}')
        rss, sockets, threads = cur.fetchone()
    assert (round(rss), sockets, threads) == (412, 24, 31)


def test_a_refused_socket_count_is_stored_as_null(clean_db):
    # None and 0 are different facts: one means "the platform would not say", the other "no
    # sockets". Collapsing them would make the Windows host look idle.
    ResourceSampleStore(clean_db).record(_sample(sockets=None))
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT open_sockets FROM {_TABLE}')
        assert cur.fetchone()[0] is None


def test_the_window_reads_back_in_order(clean_db):
    store = ResourceSampleStore(clean_db)
    for minutes, rss in ((30, 400.0), (20, 410.0), (10, 420.0)):
        store.record(_sample(minutes_ago=minutes, rss=rss))
    window = store.window(datetime.now(timezone.utc) - timedelta(hours=1))
    assert [round(s.rss_mb) for s in window] == [400, 410, 420]


def test_the_window_respects_an_upper_bound(clean_db):
    # The weekly builder reads two adjacent windows; without `until` the previous one would
    # include the current one and the delta would always read near zero.
    store = ResourceSampleStore(clean_db)
    store.record(_sample(minutes_ago=90, rss=400.0))
    store.record(_sample(minutes_ago=10, rss=500.0))
    now = datetime.now(timezone.utc)
    earlier = store.window(now - timedelta(hours=2), now - timedelta(hours=1))
    assert [round(s.rss_mb) for s in earlier] == [400]


def test_prune_deletes_only_past_the_window(clean_db):
    store = ResourceSampleStore(clean_db, retention_days=14)
    store.record(_sample(minutes_ago=60 * 24 * 20))      # 20 days old
    store.record(_sample(minutes_ago=60))                # an hour old
    assert store.prune() == 1
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM {_TABLE}')
        assert cur.fetchone()[0] == 1


def test_the_prune_runs_once_per_utc_day(clean_db):
    store = ResourceSampleStore(clean_db)
    store.record(_sample())
    pruned_on = store._pruned_on
    assert pruned_on is not None                 # the first record of the process also prunes
    store.record(_sample())
    assert store._pruned_on == pruned_on         # ...and the second does not re-prune


def test_a_broken_store_never_raises(caplog):
    # Diagnostics must not become a new cause of the outages they exist to explain.
    store = ResourceSampleStore('postgresql://nobody@127.0.0.1:1/nothing')
    store.record(_sample())                      # no exception
    assert store.prune() == 0
    assert store.window(datetime.now(timezone.utc) - timedelta(days=1)) == []
