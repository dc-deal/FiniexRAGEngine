"""Memory census (2026-09-30) — what is this process holding, and what is it costing to hold it?

The gauge (ISSUE_89) answers *whether* the process grows. On 2026-09-27..30 it grew to 7.2 GB and
froze for 10–18 min every ~6 h, and nothing reachable from outside could say *what* grew or *why*
it froze: the cause was found by reading code and reproducing it by hand. This unit is the
instrument that would have answered both over the API in minutes.

Two halves, split by cost, because a census that stops the engine is its own incident:

- **the reading** — O(1) numbers (`sys.getallocatedblocks`, `gc.get_count`/`get_stats`, the pause
  hook's figures, psycopg's class cache). Taken on the gauge's tick and logged as `[MEMORY]`.
- **the diagnosis** — walks every GC-tracked object once (top types, live counts of the objects
  this engine has leaked before) and, when enabled, a tracemalloc snapshot with growth since the
  previous call. On request only (`GET /v1/diagnose/memory`); `census_ms` says what it cost.

**The pause hook** is the part that names a freeze. A collection holds the GIL, so the whole
process stops while it runs, and a stop of 18 minutes used to arrive in the log as 18 minutes of
silence. `gc.callbacks` brackets every collection; the hook only stores numbers — it never logs,
because it runs inside the collector on whatever thread triggered it (possibly one already inside
the logging machinery). The gauge's tick reports the slowest pause afterwards.

**tracemalloc is opt-in** (`diagnostics.tracemalloc_frames`, 0 = off): tracing every allocation
costs memory and CPU for the whole process lifetime, which is right for a hunt and wrong for a
default.
"""
import gc
import logging
import ssl
import sys
import threading
import time
import tracemalloc
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.adapt import AdaptersMap

from finiexragengine.types.resource_types import (
    GcGeneration,
    GcPause,
    MemoryDiagnosis,
    MemoryReading,
    TracedLine,
    TypeCount,
)

logger = logging.getLogger(__name__)

_MB = 1024.0 * 1024.0

# Objects whose live count is worth naming individually. Two leaked here before (one SSL context
# and one opener per feed poll, 2026-09-30); the other two must stay flat in a healthy process.
_WATCHED: Tuple[Tuple[str, type], ...] = (
    ('ssl.SSLContext', ssl.SSLContext),
    ('urllib.request.OpenerDirector', urllib.request.OpenerDirector),
    ('psycopg.Connection', psycopg.Connection),
    ('threading.Thread', threading.Thread),
)

# tracemalloc's own bookkeeping and the import machinery are noise in every snapshot.
_TRACE_FILTERS = (
    tracemalloc.Filter(False, tracemalloc.__file__),
    tracemalloc.Filter(False, '<frozen importlib._bootstrap>'),
    tracemalloc.Filter(False, '<frozen importlib._bootstrap_external>'),
)


class MemoryCensus:
    """Counts what the process holds; brackets every garbage collection to time its pause."""

    def __init__(self, *, tracemalloc_frames: int = 0, top_n: int = 20) -> None:
        self._tracemalloc_frames = tracemalloc_frames
        self._top_n = top_n
        self._installed = False
        self._started_tracing = False
        # Pause bookkeeping, written by the gc hook and read by the tick. Plain attribute writes
        # under the GIL — the hook must stay allocation-light and lock-free.
        self._gc_started: Optional[float] = None
        self._last_pause_ms: Dict[int, float] = {}
        self._max_pause_ms: Dict[int, float] = {}
        self._slowest: Optional[GcPause] = None
        # The previous tracemalloc statistics, kept as {location: (size, count)} — small (one entry
        # per distinct source line), unlike a whole snapshot, and all a growth diff needs.
        self._previous_traces: Optional[Dict[str, Tuple[int, int]]] = None
        self._previous_traced_at: Optional[datetime] = None
        self._process: Optional[Any] = self._attach_process()

    @staticmethod
    def _attach_process() -> Optional[Any]:
        """psutil's view of this process, or None — the census degrades, it never breaks a boot."""
        try:
            import psutil                                  # noqa: PLC0415 — optional, as in the gauge
        except ImportError:
            return None
        return psutil.Process()

    def install(self) -> None:
        """Hook the collector and, when configured, start tracemalloc. Idempotent."""
        if self._installed:
            return
        gc.callbacks.append(self._on_gc)
        self._installed = True
        if self._tracemalloc_frames > 0 and not tracemalloc.is_tracing():
            tracemalloc.start(self._tracemalloc_frames)
            self._started_tracing = True
            logger.warning('[MEMORY] tracemalloc tracing %d frame(s) per allocation — a diagnostic '
                           'mode with memory and CPU overhead; set diagnostics.tracemalloc_frames '
                           'to 0 once the hunt is over', self._tracemalloc_frames)

    def uninstall(self) -> None:
        """Remove the hook (and stop tracing if this census started it). Idempotent."""
        if not self._installed:
            return
        try:
            gc.callbacks.remove(self._on_gc)
        except ValueError:
            pass
        self._installed = False
        if self._started_tracing:
            tracemalloc.stop()
            self._started_tracing = False

    def _on_gc(self, phase: str, info: Dict[str, int]) -> None:
        """`gc.callbacks` entry: time the collection. Numbers only — no logging, no locks."""
        if phase == 'start':
            self._gc_started = time.perf_counter()
            return
        started, self._gc_started = self._gc_started, None
        if started is None:                               # hook installed mid-collection
            return
        pause_ms = (time.perf_counter() - started) * 1000.0
        generation = int(info.get('generation', -1))
        self._last_pause_ms[generation] = pause_ms
        if pause_ms > self._max_pause_ms.get(generation, 0.0):
            self._max_pause_ms[generation] = pause_ms
        if self._slowest is None or pause_ms > self._slowest.pause_ms:
            self._slowest = GcPause(generation=generation, pause_ms=pause_ms,
                                    collected=int(info.get('collected', 0)))

    def reading(self, *, reset_slowest: bool = False) -> MemoryReading:
        """The O(1) half. `reset_slowest` hands the slowest pause to one consumer (the tick)."""
        slowest = self._slowest
        if reset_slowest:
            self._slowest = None
        generations = [
            GcGeneration(generation=index,
                         collections=int(stats.get('collections', 0)),
                         collected=int(stats.get('collected', 0)),
                         uncollectable=int(stats.get('uncollectable', 0)),
                         last_pause_ms=self._last_pause_ms.get(index),
                         max_pause_ms=self._max_pause_ms.get(index))
            for index, stats in enumerate(gc.get_stats())]
        optimised = getattr(AdaptersMap, '_optimised', None)
        return MemoryReading(ts=datetime.now(timezone.utc),
                             allocated_blocks=sys.getallocatedblocks(),
                             gc_pending=list(gc.get_count()),
                             generations=generations,
                             threads=threading.active_count(),
                             pg_adapter_classes=len(optimised) if optimised is not None else None,
                             slowest_pause=slowest)

    def diagnose(self) -> MemoryDiagnosis:
        """The expensive half: one walk over every GC-tracked object, plus tracemalloc if on."""
        started = time.perf_counter()
        rss_mb, private_mb = self._process_memory()
        by_type: Counter = Counter()
        tracked = 0
        for obj in gc.get_objects():
            by_type[type(obj)] += 1
            tracked += 1
        # Subclasses count toward their watched base: `ssl.SSLContext` instances are subclasses of
        # `_ssl._SSLContext`, and a psycopg connection may be a subclass of `Connection`.
        live = {name: sum(count for cls, count in by_type.items() if issubclass(cls, base))
                for name, base in _WATCHED}
        top_types = [TypeCount(type_name=f'{cls.__module__}.{cls.__qualname__}', count=count)
                     for cls, count in by_type.most_common(self._top_n)]
        diagnosis = MemoryDiagnosis(reading=self.reading(), rss_mb=rss_mb, private_mb=private_mb,
                                    gc_tracked_objects=tracked, top_types=top_types, live=live,
                                    census_ms=0.0)
        if tracemalloc.is_tracing():
            self._add_traces(diagnosis)
        diagnosis.census_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return diagnosis

    def _process_memory(self) -> Tuple[Optional[float], Optional[float]]:
        """(rss, private) in MB from psutil; None where unavailable or refused."""
        if self._process is None:
            return None, None
        try:
            memory = self._process.memory_info()
        except Exception:   # noqa: BLE001 — psutil raises platform-specific errors
            return None, None
        private = getattr(memory, 'private', None)
        return (round(memory.rss / _MB, 1),
                round(private / _MB, 1) if private is not None else None)

    def _add_traces(self, diagnosis: MemoryDiagnosis) -> None:
        """Top source lines by traced size, and their growth since the previous diagnosis."""
        snapshot = tracemalloc.take_snapshot().filter_traces(_TRACE_FILTERS)
        current: Dict[str, Tuple[int, int]] = {}
        for stat in snapshot.statistics('lineno'):
            frame = stat.traceback[0]
            current[f'{frame.filename}:{frame.lineno}'] = (stat.size, stat.count)
        diagnosis.tracemalloc_enabled = True
        diagnosis.traced_mb = round(tracemalloc.get_traced_memory()[0] / _MB, 1)
        by_size = sorted(current.items(), key=lambda item: item[1][0], reverse=True)
        diagnosis.traced_top = [TracedLine(location=location, size_kb=round(size / 1024.0, 1),
                                           count=count)
                                for location, (size, count) in by_size[:self._top_n]]
        previous = self._previous_traces
        if previous is not None:
            growth: List[TracedLine] = []
            for location, (size, count) in current.items():
                before_size, before_count = previous.get(location, (0, 0))
                if size > before_size:
                    growth.append(TracedLine(location=location, size_kb=round(size / 1024.0, 1),
                                             count=count,
                                             size_diff_kb=round((size - before_size) / 1024.0, 1),
                                             count_diff=count - before_count))
            growth.sort(key=lambda line: line.size_diff_kb or 0.0, reverse=True)
            diagnosis.traced_growth = growth[:self._top_n]
            diagnosis.traced_since = self._previous_traced_at
        self._previous_traces = current
        self._previous_traced_at = datetime.now(timezone.utc)
