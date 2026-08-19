"""Integration tests for SourceHealthStore — counters, the ladder, the probe, the guard.

Skipped when psycopg or a reachable PostgreSQL is missing, so the suite stays green everywhere.
No API budget is touched (the store is pure DB I/O). Runs against the canonical `source_health`
and `source_quarantine_log` tables in the isolated, migration-built test schema
(`clean_db`, ISSUE_14).

The ISSUE_84 cases mirror `test_budget_guard.py`'s suspend/cool-off/probe/resume shape on purpose:
it is the same circuit-breaker pattern applied to feeds instead of paid calls, so the tests that
prove it should read the same way.
"""
import psycopg
import pytest

from finiexragengine.core.observability.source_health_store import (
    SourceHealthStore,
    _start_rung,
)
from finiexragengine.types.config_types.app_config_types import SourceHealthConfig

_TABLE = 'source_health'
_EPISODES = 'source_quarantine_log'


@pytest.fixture
def store(clean_db: str) -> SourceHealthStore:
    config = SourceHealthConfig(flag_after_consecutive_failures=3, quarantine_hours=1,
                                recent_events_kept=5)
    return SourceHealthStore(clean_db, config)


@pytest.fixture
def ladder_store(clean_db: str) -> SourceHealthStore:
    """A three-rung ladder with a guard that needs 3 of 4 sources — the ISSUE_84 fixture."""
    config = SourceHealthConfig(flag_after_consecutive_failures=2, quarantine_hours=[1, 6, 24],
                                recent_events_kept=5, correlated_failure_ratio=0.75,
                                correlated_min_pollable=3)
    return SourceHealthStore(clean_db, config)


def _fail(store, source_id='cryptoslate', error_type='RATE_LIMITED', status=429,
          duration_ms=None, deadline_ms=None):
    return store.record_failure(source_id, 'cryptoslate.com', 'crypto_news',
                                error_type=error_type, status=status, message=f'HTTP {status}',
                                duration_ms=duration_ms, deadline_ms=deadline_ms)


def _episodes(dsn, source_id=None):
    where, params = ('WHERE source_id = %s', (source_id,)) if source_id else ('', ())
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT kind, rung, rungs_total, cooloff_hours, outcome, failed_of '
                    f'FROM {_EPISODES} {where} ORDER BY started_at, id', params)
        return cur.fetchall()


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


# --- states_of: what the envelope's `sources_reached` is measured from --------------------

def test_states_of_reads_back_what_a_reach_decision_needs(store):
    # Every way a source can be "not delivering", in one pass. The middle two are the ones the old
    # envelope arithmetic (`configured - failed_sources`) could never see: a quarantined feed is
    # not polled, so it never fails a fetch, so it used to count as reached.
    store.record_success('forexlive', 'forexlive.com', 'forex_news')
    _fail(store, 'boe_news', error_type='HTTP_ERROR', status=500)   # last poll failed, not flagged
    for _ in range(3):
        _fail(store, 'fxstreet', error_type='HTTP_ERROR', status=403)   # threshold -> quarantined

    states = store.states_of({'forexlive', 'boe_news', 'fxstreet', 'never_polled'})

    assert 'never_polled' not in states                    # no row: never polled, never delivered
    assert states['forexlive'].delivering is True
    assert states['boe_news'].delivering is False          # streak of 1, no quarantine yet
    assert states['boe_news'].quarantined_until is None
    assert states['fxstreet'].delivering is False
    assert states['fxstreet'].quarantined_until is not None
    assert (states['fxstreet'].last_error_type, states['fxstreet'].last_status) == ('HTTP_ERROR', 403)


def test_states_of_only_answers_about_what_it_was_asked(store):
    store.record_success('forexlive', 'forexlive.com', 'forex_news')
    store.record_success('cnbc_forex', 'cnbc.com', 'forex_news')

    assert set(store.states_of({'forexlive'})) == {'forexlive'}   # a sibling is not volunteered
    assert store.states_of(set()) == {}                           # empty in, empty out (no query)


def test_a_recovered_source_is_delivering_again(store):
    # Recovery is a successful poll, not merely an elapsed cool-off: record_success clears the
    # streak and the quarantine together, and only then does the feed count as delivering.
    for _ in range(3):
        _fail(store, 'fxstreet')
    assert store.states_of({'fxstreet'})['fxstreet'].delivering is False

    store.record_success('fxstreet', 'fxstreet.com', 'forex_news')
    assert store.states_of({'fxstreet'})['fxstreet'].delivering is True


# --- ISSUE_84: the ladder ------------------------------------------------------------------

@pytest.mark.parametrize('error_type,status,duration_ms,deadline_ms,expected', [
    # The load-bearing case: the SAME error type, split by how long it took. A poll that burned
    # the deadline is a feed that went quiet (transient); one that came back in milliseconds was
    # refused (durable). Without this split ecb_press and a dead host get the same 24 hours.
    ('UNREACHABLE', None, 10_000.0, 10_000.0, 0),
    ('UNREACHABLE', None, 20_000.0, 10_000.0, 0),   # the one retry doubles it — still "quiet"
    ('UNREACHABLE', None, 5.0, 10_000.0, 2),        # DNS / connection refused
    ('RATE_LIMITED', 429, 120.0, 10_000.0, 1),      # alive and talking, we are too fast
    ('HTTP_ERROR', 503, 80.0, 10_000.0, 0),         # their outage, usually short
    ('HTTP_ERROR', 403, 44.0, 10_000.0, 2),         # the cryptoslate/fxstreet case: refused
    ('PARSE_ERROR', None, 90.0, 10_000.0, 2),       # a broken body will not fix itself
    # No measurement available: read it conservatively rather than guessing, because
    # over-quarantining is the defect being fixed.
    ('UNREACHABLE', None, None, None, 0),
])
def test_failure_type_and_duration_pick_the_starting_rung(error_type, status, duration_ms,
                                                          deadline_ms, expected):
    assert _start_rung(error_type, status, duration_ms, deadline_ms, 0.7, last=2) == expected


def test_start_rung_clamps_to_a_single_rung_ladder():
    # A `quarantine_hours: 24` override is a one-rung ladder; nothing may index past it.
    assert _start_rung('HTTP_ERROR', 403, 44.0, 10_000.0, 0.7, last=0) == 0


def test_ladder_escalates_across_episodes(ladder_store, clean_db):
    # Same feed, same transient failure, three separate episodes inside the memory window.
    for expected_hours in (1, 6, 24):
        _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
        outcome = _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
        assert outcome.just_flagged is True
        assert outcome.rungs_total == 3
        # Recover, so the next failure opens a NEW episode rather than continuing this one.
        ladder_store.record_success('ecb_press', 'ecb.europa.eu', 'forex_news')
    rungs = [(rung, cooloff) for _kind, rung, _total, cooloff, _outcome, _of
             in _episodes(clean_db, 'ecb_press')]
    assert rungs == [(0, 1.0), (1, 6.0), (2, 24.0)]


def test_a_refused_feed_starts_on_the_longest_rung(ladder_store, clean_db):
    # fxstreet: HTTP 403 in 44ms, first episode. Same outcome as the old flat policy — but now
    # because the failure was read, not by accident.
    _fail(ladder_store, 'fxstreet', 'HTTP_ERROR', 403, 44.0, 10_000.0)
    outcome = _fail(ladder_store, 'fxstreet', 'HTTP_ERROR', 403, 44.0, 10_000.0)
    assert (outcome.rung, outcome.rungs_total) == (2, 3)
    assert _episodes(clean_db, 'fxstreet')[0][3] == 24.0


def test_an_integer_config_still_behaves_as_one_rung(clean_db):
    # The pre-ISSUE_84 shape must keep working: a user_configs override of `24` is a valid ladder.
    config = SourceHealthConfig(flag_after_consecutive_failures=2, quarantine_hours=24)
    store = SourceHealthStore(clean_db, config)
    for _ in range(2):
        _fail(store, 'boe_news', 'UNREACHABLE', None, 5.0, 10_000.0)   # would want the top rung
    outcome = _fail(store, 'boe_news', 'UNREACHABLE', None, 5.0, 10_000.0)
    assert (outcome.rung, outcome.rungs_total) == (0, 1)
    assert _episodes(clean_db, 'boe_news')[0][3] == 24.0


# --- ISSUE_84: the half-open probe ---------------------------------------------------------

def test_cooloff_expiry_hands_out_exactly_one_probe(ladder_store, clean_db):
    from datetime import datetime, timedelta, timezone
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    assert ladder_store.should_poll('ecb_press') is False
    # Let the cool-off elapse (in memory and in the row, as an expiry would).
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    ladder_store._quarantined['ecb_press'] = past
    assert ladder_store.should_poll('ecb_press') is True     # the single probe
    assert 'ecb_press' in ladder_store._probing


def test_a_successful_probe_closes_the_episode_and_resets(ladder_store, clean_db):
    from datetime import datetime, timedelta, timezone
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    ladder_store._quarantined['ecb_press'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    ladder_store.should_poll('ecb_press')
    ladder_store.record_success('ecb_press', 'ecb.europa.eu', 'forex_news')
    assert _episodes(clean_db, 'ecb_press')[0][4] == 'probe_ok'
    assert ladder_store.should_poll('ecb_press') is True


def test_a_failed_probe_escalates_immediately(ladder_store, clean_db):
    # The behaviour ISSUE_84 actually changes here: before, a probe failure re-applied the SAME
    # cool-off forever. One rung per failed probe is what makes a dead feed converge on the top.
    from datetime import datetime, timedelta, timezone
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    ladder_store._quarantined['ecb_press'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    ladder_store.should_poll('ecb_press')
    outcome = _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    assert outcome.just_flagged is True
    assert outcome.probe is True
    assert outcome.rung == 1                                 # escalated, not repeated
    kinds = _episodes(clean_db, 'ecb_press')
    assert [row[4] for row in kinds] == ['escalated', None]  # first closed, second running
    assert [row[1] for row in kinds] == [0, 1]


# --- ISSUE_84: the correlated-failure guard -------------------------------------------------

def _pass_with(store, failures, successes=()):
    """One ingest pass: `failures` fail at the threshold, `successes` poll fine."""
    with store.pass_scope('forex_news'):
        for source_id in failures:
            store.record_failure(source_id, f'{source_id}.test', 'forex_news',
                                 error_type='UNREACHABLE', status=None, message='timed out',
                                 duration_ms=10_000.0, deadline_ms=10_000.0)
        for source_id in successes:
            store.record_success(source_id, f'{source_id}.test', 'forex_news')


def test_a_fleet_wide_failure_quarantines_nobody(ladder_store, clean_db):
    # 2026-07-29 replayed: every pollable source fails in the same pass. Twelve of twelve failing
    # is evidence the feeds are not the problem — so nothing is flagged and no rung advances.
    feeds = ['ecb_press', 'fed_press', 'boe_news', 'forexlive']
    _pass_with(ladder_store, feeds)          # streak 1
    _pass_with(ladder_store, feeds)          # streak 2 -> would cross the threshold
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM {_TABLE} WHERE flagged')
        assert cur.fetchone()[0] == 0
    assert [row[0] for row in _episodes(clean_db)] == ['correlated']
    assert _episodes(clean_db)[0][5] == '4/4'
    # The whole set backs off once, instead of four feeds being quarantined for a day each.
    assert ladder_store.host_backoff_until() is not None
    assert ladder_store.should_poll('ecb_press') is False


def test_one_bad_feed_among_healthy_ones_is_still_quarantined(ladder_store, clean_db):
    # 2026-08-15 replayed: ecb_press fails while its peers answer. 1 of 4 is not evidence about
    # the host, so the ordinary policy applies — this is the case the guard must NOT swallow.
    _pass_with(ladder_store, ['ecb_press'], successes=['fed_press', 'boe_news', 'forexlive'])
    _pass_with(ladder_store, ['ecb_press'], successes=['fed_press', 'boe_news', 'forexlive'])
    kinds = _episodes(clean_db)
    assert [row[0] for row in kinds] == ['quarantine']
    assert kinds[0][1] == 0                                  # rung 1/3 — one hour, not a day
    assert ladder_store.should_poll('ecb_press') is False
    assert ladder_store.should_poll('fed_press') is True     # peers keep polling


def test_a_thin_pass_cannot_look_like_a_host_failure(ladder_store, clean_db):
    # Two feeds due, both fail: ratio 1.0, but two feeds are not evidence about connectivity.
    # Without the floor, a quiet pass would suppress every quarantine forever.
    _pass_with(ladder_store, ['ecb_press', 'fed_press'])
    _pass_with(ladder_store, ['ecb_press', 'fed_press'])
    assert [row[0] for row in _episodes(clean_db)] == ['quarantine', 'quarantine']


def test_connectivity_returning_closes_the_event_and_flags_only_the_dead_feeds(ladder_store,
                                                                               clean_db):
    # The partial-recovery case: ten of twelve come back, two stay dead. The ratio drops below
    # the threshold, so the event closes and the genuinely dead feeds are flagged at once —
    # carrying the streak they accumulated during the outage.
    feeds = ['ecb_press', 'fed_press', 'boe_news', 'forexlive']
    _pass_with(ladder_store, feeds)
    _pass_with(ladder_store, feeds)
    ladder_store._host_backoff_until = None                  # cool-off elapsed
    _pass_with(ladder_store, ['ecb_press'], successes=['fed_press', 'boe_news', 'forexlive'])
    rows = _episodes(clean_db)
    assert [row[0] for row in rows] == ['correlated', 'quarantine']
    assert rows[0][4] == 'resumed'                           # the event was closed by this pass
    assert ladder_store.should_poll('fed_press') is True
    assert ladder_store.should_poll('ecb_press') is False


def test_counters_survive_a_pass_that_never_resolves(ladder_store, clean_db):
    # The reason only the DECISION is deferred, not the accounting: a pass that dies mid-way
    # (timeout, crash) must still leave every failure recorded.
    try:
        with ladder_store.pass_scope('forex_news'):
            _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
            raise RuntimeError('pass died')
    except RuntimeError:
        pass
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT total_failures, consecutive_failures FROM {_TABLE} '
                    'WHERE source_id = %s', ('ecb_press',))
        assert cur.fetchone() == (1, 1)


# --- ISSUE_84 follow-ups --------------------------------------------------------------------

def test_the_rung_is_answered_without_touching_the_database(ladder_store, clean_db):
    # The ingestor asks on every skipped poll, which is the hot path `should_poll` promises to
    # keep DB-free — at a 15s cadence a query here would run ~5,760 times a day per quarantined
    # feed. Proven by making a connection fatal: if `rung_of` still answers, it never asked.
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)

    def _explode():
        raise AssertionError('rung_of must not open a connection')

    ladder_store._connect = _explode
    assert ladder_store.rung_of('ecb_press') == (0, 3)
    assert ladder_store.rung_of('never_quarantined') is None


def test_the_cached_rung_survives_a_worker_restart(ladder_store, clean_db):
    # A fresh process must be able to render "rung 1/3" too, or the marker silently degrades to
    # "quarantined" after every restart — one query for the whole fleet at boot, not one per skip.
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    reborn = SourceHealthStore(clean_db, ladder_store._config)
    assert reborn.should_poll('ecb_press') is False
    assert reborn.rung_of('ecb_press') == (0, 3)


def test_a_probe_success_drops_the_cached_rung(ladder_store, clean_db):
    from datetime import datetime, timedelta, timezone
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    _fail(ladder_store, 'ecb_press', 'UNREACHABLE', None, 10_000.0, 10_000.0)
    ladder_store._quarantined['ecb_press'] = datetime.now(timezone.utc) - timedelta(seconds=1)
    ladder_store.should_poll('ecb_press')
    ladder_store.record_success('ecb_press', 'ecb.europa.eu', 'forex_news')
    assert ladder_store.rung_of('ecb_press') is None      # no longer held, so no rung to show


def test_a_success_clears_the_error_type_it_no_longer_describes(store, clean_db):
    # `last_status` describes THIS poll; leaving `last_error_type` from an older one made healthy
    # rows read 'UNREACHABLE / 200' — two events rendered as one contradictory state.
    _fail(store, 'boe_news', error_type='HTTP_ERROR', status=500)
    store.record_success('boe_news', 'bankofengland.co.uk', 'forex_news')
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT last_error_type, last_status FROM {_TABLE} WHERE source_id = %s',
                    ('boe_news',))
        assert cur.fetchone() == (None, 200)
