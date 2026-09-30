"""Tests for the process resource gauge (ISSUE_89).

No database and no real psutil dependency in the failure cases: the gauge's *degradation* rules
are the part that decides whether a deploy boots, so they are covered everywhere.

Both rules exist because of how this engine is actually operated. A deploy is `git pull` on a live
Windows host, so a forgotten `pip install` must degrade rather than block boot; and
`Process.net_connections()` needs privileges Windows does not always grant, so a refusal must cost
one field and not the sample.
"""
import builtins
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.types.resource_types import GcPause, MemoryReading


class _FakeMemory:
    def __init__(self, rss: int) -> None:
        self.rss = rss


class _FakeWindowsMemory(_FakeMemory):
    """psutil's Windows pmem: `rss` is the working set, `private` the committed bytes."""

    def __init__(self, rss: int, private: int) -> None:
        super().__init__(rss)
        self.private = private


class _FakeProcess:
    """Stands in for psutil.Process — `sockets` may raise, as it does on Windows."""

    def __init__(self, rss_mb: float = 400.0, sockets: int = 24, threads: int = 31,
                 sockets_raise: bool = False, private_mb: Optional[float] = None) -> None:
        self._rss = int(rss_mb * 1024 * 1024)
        self._private = int(private_mb * 1024 * 1024) if private_mb is not None else None
        self._sockets = sockets
        self._threads = threads
        self._sockets_raise = sockets_raise

    def memory_info(self) -> _FakeMemory:
        if self._private is not None:
            return _FakeWindowsMemory(self._rss, self._private)
        return _FakeMemory(self._rss)

    def net_connections(self, kind: str = 'inet') -> list:
        if self._sockets_raise:
            raise PermissionError('AccessDenied')
        return [object()] * self._sockets

    def num_threads(self) -> int:
        return self._threads


def _gauge(process: _FakeProcess, **kwargs) -> ResourceGauge:
    gauge = ResourceGauge(**kwargs)
    gauge._process = process          # bypass psutil; the attach path has its own test
    gauge._enabled = True
    return gauge


def test_a_sample_carries_the_three_measurements():
    sample = _gauge(_FakeProcess(rss_mb=412.0)).sample()
    assert round(sample.rss_mb) == 412
    assert (sample.open_sockets, sample.threads) == (24, 31)
    assert sample.ts.tzinfo is not None          # UTC-aware, like every timestamp here


def test_a_refused_socket_count_costs_that_field_not_the_sample():
    # Windows / restricted containers refuse net_connections(). Memory is what the 2026-08-01
    # incident was about, so losing the whole sample over the socket column would be backwards.
    sample = _gauge(_FakeProcess(sockets_raise=True)).sample()
    assert sample.open_sockets is None
    assert sample.rss_mb > 0 and sample.threads == 31


def test_a_missing_psutil_disables_the_gauge_instead_of_raising(monkeypatch, caplog):
    # The failure mode this design exists to avoid: a deploy that forgot `pip install` must not
    # take the engine down over a diagnostic.
    real_import = builtins.__import__

    def _no_psutil(name, *args, **kwargs):
        if name == 'psutil':
            raise ImportError('No module named psutil')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', _no_psutil)
    with caplog.at_level(logging.WARNING):
        gauge = ResourceGauge()
    assert gauge.enabled is False
    assert gauge.sample() is None                # every later call a no-op
    assert gauge.status()['enabled'] is False
    assert 'psutil' in caplog.text


def test_a_disabled_gauge_never_samples():
    gauge = _gauge(_FakeProcess())
    gauge._enabled = False
    assert gauge.sample() is None


def test_a_failing_process_read_is_swallowed():
    # The caller is the stall watchdog's tick, and a watchdog that dies is worse than one that errs.
    class _Broken(_FakeProcess):
        def memory_info(self):
            raise RuntimeError('psutil exploded')

    assert _gauge(_Broken()).sample() is None


def test_the_ceiling_warns_on_the_edge_not_on_every_tick(caplog):
    gauge = _gauge(_FakeProcess(rss_mb=500.0), rss_warn_mb=400)
    with caplog.at_level(logging.WARNING):
        gauge.sample()
        gauge.sample()
        gauge.sample()
    # At a 60s tick a level-triggered warning would be 1,440 identical lines a day — the exact
    # shape of noise ISSUE_84 spent a batch removing from the source logs.
    assert caplog.text.count('crossed the 400 MB ceiling') == 1
    assert gauge.over_ceiling is True


def test_dropping_back_under_the_ceiling_is_reported_and_re_arms(caplog):
    gauge = _gauge(_FakeProcess(rss_mb=500.0), rss_warn_mb=400)
    gauge.sample()
    gauge._process = _FakeProcess(rss_mb=300.0)
    with caplog.at_level(logging.INFO):
        gauge.sample()
    assert 'back under' in caplog.text
    assert gauge.over_ceiling is False
    gauge._process = _FakeProcess(rss_mb=500.0)
    with caplog.at_level(logging.WARNING):
        gauge.sample()
    assert 'crossed the 400 MB ceiling' in caplog.text     # re-armed, so a second episode speaks


def test_no_ceiling_configured_means_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        _gauge(_FakeProcess(rss_mb=5000.0), rss_warn_mb=0).sample()
    assert 'ceiling' not in caplog.text
    # 0 is the shipped default on purpose: the only datapoint is 1,191 MB on a FROZEN process,
    # which is not a baseline. The weekly line is what produces the number to set it from.


def test_a_store_error_never_reaches_the_caller():
    class _BrokenStore:
        def record(self, sample):
            raise RuntimeError('db down')

    gauge = _gauge(_FakeProcess())
    gauge._store = _BrokenStore()
    # The store swallows by contract; if one ever leaks, the gauge must still not take the tick.
    try:
        sample = gauge.sample()
    except RuntimeError:
        sample = None
    assert sample is None or sample.rss_mb > 0


def test_status_shape_for_health():
    gauge = _gauge(_FakeProcess(rss_mb=412.34), rss_warn_mb=800)
    gauge.sample()
    status = gauge.status()
    assert status['enabled'] is True and status['ceiling_mb'] == 800
    assert status['rss_mb'] == 412.3 and status['open_sockets'] == 24
    assert status['over_ceiling'] is False
    assert status['sampled_at'] is not None


def test_private_bytes_are_carried_where_the_platform_reports_them() -> None:
    # 2026-09-30: the working set read 5.2 GB while 7.2 GB were committed — the gap IS the paging.
    windows = _gauge(_FakeProcess(rss_mb=5200.0, private_mb=7200.0)).sample()
    assert round(windows.private_mb) == 7200 and round(windows.rss_mb) == 5200
    elsewhere = _gauge(_FakeProcess(rss_mb=412.0)).sample()
    assert elsewhere.private_mb is None               # not exposed there: unknown, never zero


def test_the_ceiling_reads_private_bytes_where_rss_is_a_trimmed_working_set(caplog) -> None:
    gauge = _gauge(_FakeProcess(rss_mb=300.0, private_mb=600.0), rss_warn_mb=500)
    with caplog.at_level(logging.WARNING):
        gauge.sample()
    assert gauge.over_ceiling is True
    assert 'private 600 MB crossed the 500 MB ceiling' in caplog.text


def test_status_carries_private_bytes_for_health() -> None:
    gauge = _gauge(_FakeProcess(rss_mb=300.0, private_mb=612.34))
    gauge.sample()
    assert gauge.status()['private_mb'] == 612.3


class _StubCensus:
    """Hands the gauge a prepared reading; records whether the tick reset the slowest pause."""

    def __init__(self, pause_ms: Optional[float] = None) -> None:
        self._pause = GcPause(generation=2, pause_ms=pause_ms, collected=41) if pause_ms else None
        self.resets = 0

    def reading(self, *, reset_slowest: bool = False) -> MemoryReading:
        self.resets += int(reset_slowest)
        return MemoryReading(ts=datetime.now(timezone.utc), allocated_blocks=1_234_567,
                             gc_pending=[10, 2, 1], generations=[], threads=11,
                             pg_adapter_classes=391, slowest_pause=self._pause)


def test_a_long_collection_is_named_after_the_fact(caplog) -> None:
    census = _StubCensus(pause_ms=17 * 60 * 1000.0)   # the 17-minute freeze, as it would now read
    gauge = _gauge(_FakeProcess(), census=census, gc_pause_warn_seconds=5.0)
    with caplog.at_level(logging.WARNING):
        gauge.sample()
    assert '[GC] generation 2 collection froze the process for 1020.0 s' in caplog.text
    assert census.resets == 1                         # the tick owns the slowest pause


def test_a_short_collection_stays_quiet(caplog) -> None:
    gauge = _gauge(_FakeProcess(), census=_StubCensus(pause_ms=120.0), gc_pause_warn_seconds=5.0)
    with caplog.at_level(logging.WARNING):
        gauge.sample()
    assert '[GC]' not in caplog.text


def test_the_memory_line_is_logged_on_its_cadence_not_every_tick(caplog) -> None:
    gauge = _gauge(_FakeProcess(rss_mb=380.0, private_mb=412.0), census=_StubCensus(),
                   memory_log_minutes=10)
    with caplog.at_level(logging.INFO):
        gauge.sample()
        gauge.sample()                                # same minute: no second line
    lines = [record.message for record in caplog.records if record.message.startswith('[MEMORY]')]
    assert len(lines) == 1
    assert 'private 412 MB · rss 380 MB · blocks 1,234,567' in lines[0]
    assert 'pg adapter classes 391 · threads 11' in lines[0]

    gauge._last_memory_log -= timedelta(minutes=11)   # the cadence has elapsed
    caplog.clear()
    with caplog.at_level(logging.INFO):
        gauge.sample()
    assert any(record.message.startswith('[MEMORY]') for record in caplog.records)


def test_no_census_means_no_memory_line(caplog) -> None:
    with caplog.at_level(logging.INFO):
        _gauge(_FakeProcess(), memory_log_minutes=10).sample()
    assert '[MEMORY]' not in caplog.text
