"""Tests for the memory census (2026-09-30) — what the process holds, and how long collections stall it.

No database, no psutil dependency in the logic under test. The census exists because a 7 GB growth
and 18-minute freezes were diagnosed by reading code instead of by asking the process; these tests
pin the instruments that make the process answer: the collector hook, the O(1) reading, the object
walk, and the tracemalloc growth diff.
"""
import gc
import ssl
import tracemalloc
from typing import Iterator, List

import pytest

from finiexragengine.core.observability.memory_census import MemoryCensus


@pytest.fixture
def census() -> Iterator[MemoryCensus]:
    instance = MemoryCensus()
    yield instance
    instance.uninstall()                              # never leave a hook in gc.callbacks


def test_install_hooks_the_collector_once_and_uninstall_removes_it(census: MemoryCensus) -> None:
    census.install()
    census.install()                                  # idempotent: a second call adds nothing
    assert gc.callbacks.count(census._on_gc) == 1
    census.uninstall()
    assert census._on_gc not in gc.callbacks


def test_a_collection_is_timed_and_handed_to_one_consumer(census: MemoryCensus) -> None:
    census.install()
    gc.collect()                                      # a full, generation-2 collection

    reading = census.reading(reset_slowest=True)
    full = reading.generations[2]
    assert full.last_pause_ms is not None and full.last_pause_ms >= 0.0
    assert full.max_pause_ms is not None and full.max_pause_ms >= full.last_pause_ms
    assert reading.slowest_pause is not None and reading.slowest_pause.generation == 2
    # Reset means the tick owns it: the next reading reports no pause until another collection.
    assert census.reading().slowest_pause is None


def test_without_the_hook_pauses_are_unknown_rather_than_zero(census: MemoryCensus) -> None:
    gc.collect()
    reading = census.reading()
    assert all(generation.last_pause_ms is None for generation in reading.generations)
    assert reading.slowest_pause is None


def test_the_reading_carries_the_cheap_numbers(census: MemoryCensus) -> None:
    reading = census.reading()
    assert reading.allocated_blocks > 0
    assert len(reading.gc_pending) == 3 and len(reading.generations) == 3
    assert reading.threads >= 1
    assert isinstance(reading.pg_adapter_classes, int)   # psycopg's class cache, watched for B


def test_the_diagnosis_counts_the_objects_that_leaked_before(census: MemoryCensus) -> None:
    held: List[ssl.SSLContext] = [ssl.create_default_context() for _ in range(3)]
    diagnosis = census.diagnose()

    assert diagnosis.live['ssl.SSLContext'] >= len(held)
    assert set(diagnosis.live) == {'ssl.SSLContext', 'urllib.request.OpenerDirector',
                                   'psycopg.Connection', 'threading.Thread'}
    assert diagnosis.gc_tracked_objects > 0 and diagnosis.top_types
    assert diagnosis.top_types[0].count >= diagnosis.top_types[-1].count
    assert diagnosis.census_ms >= 0.0                 # what the walk itself cost, always stated
    assert diagnosis.tracemalloc_enabled is False     # opt-in, off by default
    del held


def test_tracemalloc_reports_growth_since_the_previous_diagnosis() -> None:
    if tracemalloc.is_tracing():
        pytest.skip('tracemalloc already running (e.g. PYTHONTRACEMALLOC) — cannot own it here')
    census = MemoryCensus(tracemalloc_frames=1)
    census.install()
    try:
        first = census.diagnose()
        assert first.tracemalloc_enabled and first.traced_top
        assert first.traced_growth == [] and first.traced_since is None   # nothing to diff yet

        grown = [bytearray(64 * 1024) for _ in range(64)]                  # ~4 MB held right here
        second = census.diagnose()
        assert second.traced_since is not None
        here = [line for line in second.traced_growth if __file__ in line.location]
        assert here and here[0].size_diff_kb >= 3000
        del grown
    finally:
        census.uninstall()
    assert not tracemalloc.is_tracing()               # the census stops what it started
