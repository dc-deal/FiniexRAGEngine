"""EngineStats — the live dashboard's shared state (ISSUE_26): per-worker keys, bounded stream."""
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from finiexragengine.core.ui.engine_stats import (
    EngineStats,
    IngestSnapshot,
    SourcesSnapshot,
)

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _ingest(new: int) -> IngestSnapshot:
    return IngestSnapshot(last=_NOW, fetched=new, new=new, cost_usd=0.0, duration_ms=1.0)


def test_keys_are_pre_registered_as_idle():
    stats = EngineStats(source_set_ids=['crypto_news', 'forex_news'],
                        pipeline_ids=['crypto_sentiment'])
    # Every worker's key exists up front (None = idle) so its row never vanishes before it runs.
    assert set(stats.ingest()) == {'crypto_news', 'forex_news'}
    assert stats.ingest()['crypto_news'] is None
    assert set(stats.llm()) == {'crypto_sentiment'}


def test_set_replaces_only_its_own_key():
    stats = EngineStats(source_set_ids=['crypto_news'])
    stats.set_ingest('crypto_news', _ingest(3))
    stats.set_ingest('crypto_news', _ingest(7))
    assert stats.ingest()['crypto_news'].new == 7            # last writer wins, per key


def test_two_workers_do_not_clobber_each_other():
    """The bug this design fixes: two ingest workers must not overwrite one shared slot."""
    stats = EngineStats(source_set_ids=['crypto_news', 'forex_news'])
    stats.set_ingest('crypto_news', _ingest(119))
    stats.set_ingest('forex_news', _ingest(69))
    assert stats.ingest()['crypto_news'].new == 119          # both survive, distinctly
    assert stats.ingest()['forex_news'].new == 69


def test_breaking_counters_accumulate():
    stats = EngineStats()
    assert stats.breaking().detected == 0 and stats.breaking().confirmed == 0
    stats.add_breaking_detected(2, at=_NOW)
    stats.add_breaking_detected(1, at=_NOW)
    stats.add_breaking_episode('ADAUSD', 'SELL', 'greed spike', 'engine 42s / e2e 3.1m', at=_NOW)
    stats.add_breaking_episode('ETHUSD', 'BUY', 'ETF inflows', 'engine 12s / e2e 30s', at=_NOW)
    breaking = stats.breaking()
    assert breaking.detected == 3                             # cumulative, engine-wide
    assert breaking.confirmed == 2                            # one per episode (edge-triggered)
    assert breaking.detail == 'engine 12s / e2e 30s'         # last episode's reaction
    # The BREAKING section keeps the episodes (oldest→newest) with their reason (ISSUE_64).
    recent = stats.recent_breaking()
    assert [(r.symbol, r.signal, r.reason) for r in recent] == [
        ('ADAUSD', 'SELL', 'greed spike'), ('ETHUSD', 'BUY', 'ETF inflows')]


def test_touch_advances_last_seen_but_freezes_the_start():
    """An ongoing episode grows its duration (last_seen moves) while its start stays fixed (ISSUE_64)."""
    from datetime import timedelta
    stats = EngineStats()
    stats.add_breaking_episode('ADAUSD', 'SELL', 'greed', 'd', at=_NOW)
    later = _NOW + timedelta(minutes=10)
    stats.touch_breaking_episode('ADAUSD', at=later)
    record = stats.recent_breaking()[-1]
    assert record.started == _NOW and record.last_seen == later
    stats.touch_breaking_episode('UNKNOWN', at=later)        # no open record → harmless no-op
    assert len(stats.recent_breaking()) == 1


def test_event_stream_is_capped_at_maxlen():
    stats = EngineStats(max_events=5)
    for i in range(10):
        stats.push_event('INGEST', f'pass {i}')
    events = stats.events()
    assert len(events) == 5                                   # O(1) memory regardless of uptime
    assert [e.message for e in events] == [f'pass {i}' for i in range(5, 10)]  # oldest fell off


def test_concurrent_writer_and_reader_never_tear():
    """A worker thread writes while the render loop reads/iterates — lock-free, must never raise."""
    stats = EngineStats(source_set_ids=['crypto_news', 'forex_news'], max_events=100)
    stop = threading.Event()

    def writer() -> None:
        n = 0
        while not stop.is_set():
            stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=n, total=6))
            stats.set_sources('forex_news', SourcesSnapshot(last=_NOW, ok=n, total=7))
            stats.push_event('INGEST', f'pass {n}')
            n += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(2000):
            # Iterate the keyed dict (fixed size — pre-registered keys) while it is written.
            for snapshot in stats.sources().values():
                assert snapshot is None or snapshot.total in (6, 7)
            _ = stats.events()                                # a stable copy, never mid-append
    finally:
        stop.set()
        thread.join()


# --- ISSUE_74: the counters guard themselves now --------------------------------------------


@contextmanager
def _aggressive_thread_switching():
    """Make the GIL switch far more often than its 5ms default.

    Without this the tests below pass with *or* without the counter lock: a read-modify-write is
    only a handful of bytecodes, so the interpreter almost never switches inside one and the race
    stays invisible. Measured here at a 1µs interval, the unlocked version loses roughly two
    thirds of its updates — which is what gives these tests teeth instead of false comfort.
    """
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def test_concurrent_counter_writes_do_not_lose_updates():
    """`add_breaking_detected` is read-modify-write.

    It used to be serialized by the workers' shared `pass_lock` — the lock ISSUE_74 removed
    because it let one hung feed stop the whole engine. EngineStats now owns a small lock of its
    own for exactly these writers, so concurrent ingest workers cannot lose a flagged candidate.
    """
    stats = EngineStats(source_set_ids=['a'], pipeline_ids=['p'])
    with _aggressive_thread_switching():
        threads = [threading.Thread(target=lambda: [stats.add_breaking_detected(1, at=_NOW)
                                                    for _ in range(2000)])
                   for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert stats.breaking().detected == 8 * 2000


def test_concurrent_episode_writes_keep_count_and_deque_consistent():
    # Same guarantee for the other two accumulating writers, which also share the bounded deque.
    stats = EngineStats(source_set_ids=['a'], pipeline_ids=['p'])

    def add(symbol: str) -> None:
        for _ in range(500):
            stats.add_breaking_episode(symbol, 'SELL', 'why', 'engine 1m', at=_NOW)
            stats.touch_breaking_episode(symbol, at=_NOW)

    with _aggressive_thread_switching():
        threads = [threading.Thread(target=add, args=(f'SYM{i}',)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert stats.breaking().confirmed == 4 * 500
    assert len(stats.recent_breaking()) == 6            # the bounded deque held its cap


def test_restoring_an_episode_shows_it_without_touching_the_session_counters():
    """ISSUE_82: a story running across a restart stays on the panel, but is not re-counted.

    Inflating `confirmed` on every boot is the defect the seeded rule removed one layer down; the
    counters mean "what this process saw" and must keep meaning that.
    """
    from datetime import datetime, timedelta, timezone

    stats = EngineStats(source_set_ids=['crypto_news'], pipeline_ids=['crypto_sentiment'])
    started = datetime(2026, 8, 18, 15, 20, tzinfo=timezone.utc)
    last_seen = started + timedelta(hours=6)
    stats.restore_breaking_episode('USDCAD', 'SELL', 'tariffs',
                                   started=started, last_seen=last_seen, gap_seconds=9000.0)

    records = stats.recent_breaking()
    assert len(records) == 1
    assert records[0].symbol == 'USDCAD' and records[0].started == started
    assert records[0].last_seen == last_seen           # the inherited clock, not the boot time
    assert records[0].started_bounded is False         # a full window observed the real start
    # Accumulators stay session-scoped, or every boot would re-count what it inherited.
    assert stats.breaking().confirmed == 0 and stats.breaking().detected == 0
    # `last` is a fact about the world and IS restored — without it the row header read `idle`
    # directly above an episode marked live.
    assert stats.breaking().last == last_seen
    # The reaction is NOT: the replay re-opens an older episode at the window edge, so any number
    # here would be re-sampled against stale evidence (production showed 118.2m for a logged 8.4m).
    assert stats.breaking().detail == ''
