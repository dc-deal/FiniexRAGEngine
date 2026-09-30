"""Process resource gauge (ISSUE_89) — is this process growing?

The question nobody could answer on 2026-08-01, when the frozen process showed 5 sockets in
`CLOSE_WAIT` and 1,191 MB resident memory and neither number was recorded anywhere. This samples
the running process on the stall watchdog's existing tick, keeps the latest reading for `/health`,
and hands each one to the store so a *series* exists the next time the question comes up.

Two degradation rules, both deliberate:

- **A missing `psutil` disables the gauge, it never raises.** The package is a declared dependency,
  but a deploy is `git pull` on a live host and forgetting `pip install -r requirements.txt` is
  exactly the kind of thing that happens. A *diagnostic* must not be the reason the engine fails to
  boot — the same judgement `diagnostics.poll_log_enabled` encodes as a switch.
- **A refused socket count degrades that field, not the sample.** `Process.net_connections()`
  needs privileges some platforms do not grant, and the live host is Windows. `memory_info().rss`
  and `num_threads()` are cheap and unprivileged everywhere, and memory is what the incident was
  about — so a partial sample is worth strictly more than none.

The ceiling warns **once** while it is crossed rather than every tick: a watchdog-cadence alarm
would produce 1,440 identical lines a day, which is the shape of noise ISSUE_84 spent a batch
removing from the source logs.

**Private bytes, not only `rss` (2026-09-30).** On Windows `rss` is the working set, which the OS
trims under memory pressure — the gauge read 5.2 GB while the process had 7.2 GB committed. So a
sample carries `private_mb` where the platform exposes it, and the ceiling compares against it.

**The census rides the same tick** (2026-09-30): an O(1) reading every tick, logged as one
`[MEMORY]` line every `memory_log_minutes`, and a `[GC]` warning whenever a collection froze the
process for longer than `gc_pause_warn_seconds` — the line that would have named the 18-minute
freezes instead of leaving 18 minutes of silence in the log.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from finiexragengine.core.observability.memory_census import MemoryCensus
from finiexragengine.core.observability.resource_sample_store import ResourceSampleStore
from finiexragengine.types.resource_types import MemoryReading, ResourceSample

logger = logging.getLogger(__name__)

_MB = 1024.0 * 1024.0


class ResourceGauge:
    """Samples the running process, keeps the latest reading, and persists the series."""

    def __init__(self, *, store: Optional[ResourceSampleStore] = None,
                 enabled: bool = True, rss_warn_mb: int = 0,
                 census: Optional[MemoryCensus] = None, memory_log_minutes: int = 0,
                 gc_pause_warn_seconds: float = 0.0) -> None:
        # `store` is optional so the gauge still answers /health on a database-less run; the
        # series is the durable half, not the live one.
        self._store = store
        self._rss_warn_mb = rss_warn_mb
        self._latest: Optional[ResourceSample] = None
        self._over_ceiling = False
        self._process: Optional[Any] = None
        # The census (2026-09-30) is optional for the same reason the store is: the gauge must
        # keep answering /health without it. 0 disables the log line / the pause warning.
        self._census = census
        self._memory_log_minutes = memory_log_minutes
        self._gc_pause_warn_ms = gc_pause_warn_seconds * 1000.0
        self._last_memory_log: Optional[datetime] = None
        self._enabled = enabled and self._attach()

    def _attach(self) -> bool:
        """Bind to this process via psutil, or disable the gauge with one honest log line."""
        try:
            import psutil                                  # noqa: PLC0415 — optional by design
        except ImportError:
            logger.warning('[RESOURCE] gauge disabled: psutil is not installed '
                           '(pip install -r requirements.txt) — diagnostics only, engine unaffected')
            return False
        self._process = psutil.Process()
        return True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def latest(self) -> Optional[ResourceSample]:
        """The most recent reading — what /health serves, never a database round-trip."""
        return self._latest

    def sample(self) -> Optional[ResourceSample]:
        """Take one reading, remember it, persist it. Returns None while disabled.

        Never raises: the caller is the stall watchdog's tick, and a watchdog that dies is worse
        than one that errs.
        """
        if not self._enabled or self._process is None:
            return None
        try:
            memory = self._process.memory_info()
            # `private` exists on Windows only (psutil's pmem there); elsewhere the field stays None.
            private = getattr(memory, 'private', None)
            sample = ResourceSample(ts=datetime.now(timezone.utc),
                                    rss_mb=memory.rss / _MB,
                                    open_sockets=self._sockets(),
                                    threads=self._process.num_threads(),
                                    private_mb=private / _MB if private is not None else None)
        except Exception as exc:   # noqa: BLE001 — psutil raises platform-specific errors
            logger.warning('[RESOURCE] sample failed (diagnostics only): %s', exc)
            return None
        self._latest = sample
        self._check_ceiling(sample)
        if self._store is not None:
            self._store.record(sample)      # swallows its own DB errors by contract
        if self._census is not None:
            self._report_memory(sample)
        return sample

    def _report_memory(self, sample: ResourceSample) -> None:
        """The census on the tick: warn about a long GC pause, log the `[MEMORY]` line on cadence.

        Never raises — same contract as `sample()`, whose caller is the watchdog's tick.
        """
        try:
            reading = self._census.reading(reset_slowest=True)
        except Exception as exc:   # noqa: BLE001 — a diagnostic must not take the tick down
            logger.warning('[MEMORY] census reading failed (diagnostics only): %s', exc)
            return
        pause = reading.slowest_pause
        if pause is not None and 0 < self._gc_pause_warn_ms <= pause.pause_ms:
            # Reported after the fact by design: during the pause nothing runs, this tick included.
            logger.warning('[GC] generation %d collection froze the process for %.1f s '
                           '(collected %d objects) — every thread, the workers included, stood '
                           'still for that long', pause.generation, pause.pause_ms / 1000.0,
                           pause.collected)
        if self._memory_log_minutes <= 0:
            return
        due = (self._last_memory_log is None or
               (sample.ts - self._last_memory_log).total_seconds() >= self._memory_log_minutes * 60)
        if due:
            self._last_memory_log = sample.ts
            logger.info('[MEMORY] %s', _memory_line(sample, reading))

    def _sockets(self) -> Optional[int]:
        """This process's socket count, or None where the platform refuses to say."""
        try:
            return len(self._process.net_connections(kind='inet'))
        except Exception:   # noqa: BLE001 — AccessDenied on Windows / restricted containers
            return None

    def _check_ceiling(self, sample: ResourceSample) -> None:
        """Warn once on crossing, and once again on the way back — edges, not levels.

        Compared against private bytes where the platform reports them: on Windows `rss` is the
        trimmed working set and would stay under the ceiling while the process pages out.
        """
        if self._rss_warn_mb <= 0:
            return
        measured, label = ((sample.private_mb, 'private') if sample.private_mb is not None
                           else (sample.rss_mb, 'rss'))
        over = measured >= self._rss_warn_mb
        if over and not self._over_ceiling:
            logger.warning('[RESOURCE] %s %.0f MB crossed the %d MB ceiling '
                           '(sockets %s, threads %s)', label, measured, self._rss_warn_mb,
                           sample.open_sockets, sample.threads)
        elif not over and self._over_ceiling:
            logger.info('[RESOURCE] %s %.0f MB back under the %d MB ceiling',
                        label, measured, self._rss_warn_mb)
        self._over_ceiling = over

    @property
    def over_ceiling(self) -> bool:
        return self._over_ceiling

    def status(self) -> dict:
        """Gauge state for /health (ISSUE_89) — the live sample, never the table."""
        latest = self._latest
        return {
            'enabled': self._enabled,
            'rss_mb': round(latest.rss_mb, 1) if latest else None,
            'private_mb': (round(latest.private_mb, 1)
                           if latest and latest.private_mb is not None else None),
            'open_sockets': latest.open_sockets if latest else None,
            'threads': latest.threads if latest else None,
            'sampled_at': latest.ts.isoformat() if latest else None,
            'ceiling_mb': self._rss_warn_mb,
            'over_ceiling': self._over_ceiling,
        }


def _memory_line(sample: ResourceSample, reading: MemoryReading) -> str:
    """One `[MEMORY]` line: the numbers that tell a leak of objects from a leak of native memory."""
    private = f'{sample.private_mb:.0f} MB' if sample.private_mb is not None else 'n/a'
    collections = '/'.join(str(generation.collections) for generation in reading.generations)
    worst = max((generation.max_pause_ms or 0.0 for generation in reading.generations), default=0.0)
    adapters = reading.pg_adapter_classes if reading.pg_adapter_classes is not None else 'n/a'
    return (f'private {private} · rss {sample.rss_mb:.0f} MB · blocks {reading.allocated_blocks:,} '
            f'· gc runs {collections} · longest pause {worst / 1000.0:.2f} s '
            f'· pg adapter classes {adapters} · threads {reading.threads}')
