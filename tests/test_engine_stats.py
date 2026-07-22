"""EngineStats — the live dashboard's shared state (ISSUE_26): per-worker keys, bounded stream."""
import threading
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
    stats.add_breaking_confirmed(1, 'engine 42s / e2e 3.1m', at=_NOW)
    breaking = stats.breaking()
    assert breaking.detected == 3                             # cumulative, engine-wide
    assert breaking.confirmed == 1
    assert breaking.detail == 'engine 42s / e2e 3.1m'


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
