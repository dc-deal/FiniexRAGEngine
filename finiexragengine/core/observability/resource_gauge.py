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
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from finiexragengine.core.observability.resource_sample_store import ResourceSampleStore
from finiexragengine.types.resource_types import ResourceSample

logger = logging.getLogger(__name__)

_MB = 1024.0 * 1024.0


class ResourceGauge:
    """Samples the running process, keeps the latest reading, and persists the series."""

    def __init__(self, *, store: Optional[ResourceSampleStore] = None,
                 enabled: bool = True, rss_warn_mb: int = 0) -> None:
        # `store` is optional so the gauge still answers /health on a database-less run; the
        # series is the durable half, not the live one.
        self._store = store
        self._rss_warn_mb = rss_warn_mb
        self._latest: Optional[ResourceSample] = None
        self._over_ceiling = False
        self._process: Optional[Any] = None
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
            sample = ResourceSample(ts=datetime.now(timezone.utc),
                                    rss_mb=memory.rss / _MB,
                                    open_sockets=self._sockets(),
                                    threads=self._process.num_threads())
        except Exception as exc:   # noqa: BLE001 — psutil raises platform-specific errors
            logger.warning('[RESOURCE] sample failed (diagnostics only): %s', exc)
            return None
        self._latest = sample
        self._check_ceiling(sample)
        if self._store is not None:
            self._store.record(sample)      # swallows its own DB errors by contract
        return sample

    def _sockets(self) -> Optional[int]:
        """This process's socket count, or None where the platform refuses to say."""
        try:
            return len(self._process.net_connections(kind='inet'))
        except Exception:   # noqa: BLE001 — AccessDenied on Windows / restricted containers
            return None

    def _check_ceiling(self, sample: ResourceSample) -> None:
        """Warn once on crossing, and once again on the way back — edges, not levels."""
        if self._rss_warn_mb <= 0:
            return
        over = sample.rss_mb >= self._rss_warn_mb
        if over and not self._over_ceiling:
            logger.warning('[RESOURCE] rss %.0f MB crossed the %d MB ceiling '
                           '(sockets %s, threads %s)', sample.rss_mb, self._rss_warn_mb,
                           sample.open_sockets, sample.threads)
        elif not over and self._over_ceiling:
            logger.info('[RESOURCE] rss %.0f MB back under the %d MB ceiling',
                        sample.rss_mb, self._rss_warn_mb)
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
            'open_sockets': latest.open_sockets if latest else None,
            'threads': latest.threads if latest else None,
            'sampled_at': latest.ts.isoformat() if latest else None,
            'ceiling_mb': self._rss_warn_mb,
            'over_ceiling': self._over_ceiling,
        }
