"""Worker liveness watchdog (ISSUE_75) — raises its voice when the engine stops working.

The blind spot this closes: on 2026-08-01 an un-timeouted feed fetch (ISSUE_73) blocked a worker
thread that held the lock shared by all four workers, and the engine produced nothing for nine
days. Nothing failed, so nothing was logged; the process stayed alive and every *other* surface —
the API, the dashboard, the weekly cron — kept working and kept looking healthy. Absence of work
is not an error anywhere, so it has to be watched for deliberately.

Sibling of `budget_guard.py` by design, and deliberately the same character: watch a condition,
hold episode state, act **once** on the transition, and expose a `status()` the display and
/health render. The detection itself is `check()` — pure, clock-injected, and where the tests
live; `run()`/`stop()` only drive it on an interval, mirroring `LiveDisplay`.

Delivery is injected as a callback rather than imported: the watchdog never learns what Telegram
is, which keeps it testable and keeps `observability/` free of an `alerts/` dependency.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set

from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.types.alert_types import AlertCallback
from finiexragengine.types.config_types.app_config_types import StallWatchdogConfig
from finiexragengine.types.worker_types import WorkerState

logger = logging.getLogger(__name__)


def _format_age(seconds: float) -> str:
    """Compact relative age: `45s` · `15m` · `9h22m` — the live display's vocabulary."""
    if seconds < 90:
        return f'{seconds:.0f}s'
    if seconds < 3600:
        return f'{seconds / 60:.0f}m'
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f'{hours}h{minutes:02d}m'


@dataclass
class StallEvent:
    """One transition — a worker entering or leaving a stall. Never one per tick."""
    worker: str
    stalled: bool             # True = entered a stall, False = recovered
    silent_seconds: float     # how long the worker had been silent at the transition
    threshold_seconds: float
    last_run_at: Optional[datetime]

    def message(self, running: int, total: int) -> str:
        """The operator-facing line — same text for the log and the alert sink."""
        if not self.stalled:
            return (f'✅ FiniexRAGEngine — worker recovered\n'
                    f'{self.worker} · resumed after {_format_age(self.silent_seconds)}')
        last = self.last_run_at.strftime('%Y-%m-%d %H:%M:%S UTC') if self.last_run_at else 'never'
        return (f'⚠️ FiniexRAGEngine — worker stalled\n'
                f'{self.worker} · no completed pass for {_format_age(self.silent_seconds)}\n'
                f'last ok: {last}\n'
                f'{running} of {total} workers still running')


class StallWatchdog:
    """Flags a worker that has not completed a pass within its own threshold.

    Threshold per worker is `max(factor x cadence, floor_minutes)` — the floor is load-bearing,
    not a safety margin: the ingest cadence is 15s, so a bare factor would fire after 45 seconds
    and turn one slow pass into an alarm. `WorkerState.last_run_at` is stamped when a pass
    *starts*, which is the honest anchor here: a worker that started a pass and never returned is
    exactly the failure being watched for, and its timestamp stops advancing either way.
    """

    def __init__(self, config: StallWatchdogConfig,
                 states_provider: Callable[[], List[WorkerState]],
                 alert: Optional[AlertCallback] = None,
                 gauge: Optional[ResourceGauge] = None) -> None:
        self._config = config
        # A provider, not a snapshot: the supervisor's WorkerState objects are mutated in place
        # by the workers, and re-asking each tick keeps this unit from holding engine internals.
        self._states_provider = states_provider
        self._alert = alert
        # Optional (ISSUE_89): the process resource gauge, sampled on this watchdog's tick.
        # None = no sampling; the liveness job is unchanged either way.
        self._gauge = gauge
        # Which workers are currently *known* to be stalled — the episode memory that makes this
        # edge-triggered (one line per stall, one on recovery) instead of a per-tick repeat.
        self._stalled: Set[str] = set()
        self._stop = asyncio.Event()

    def set_gauge(self, gauge: Optional[ResourceGauge]) -> None:
        """Sample process resources on this watchdog's tick (ISSUE_89).

        Injected rather than constructed here: the watchdog's job is worker liveness, and it must
        stay buildable without a gauge — the same reason `set_alert` exists. The tick is reused
        because it already runs on the right cadence (60s) and already guarantees that a failing
        tick does not kill the loop; a second async loop would only duplicate both.
        """
        self._gauge = gauge

    def set_alert(self, alert: Optional[AlertCallback]) -> None:
        """Attach the delivery sink after construction — the Telegram client is built later in
        the boot sequence than the workers this watches, and detection must not wait on it."""
        self._alert = alert

    def threshold_seconds(self, state: WorkerState) -> float:
        return max(self._config.factor * state.interval_seconds,
                   self._config.floor_minutes * 60)

    def stalled_workers(self) -> Set[str]:
        """The worker names currently in a stall — the live display's single source of truth."""
        return set(self._stalled)

    def status(self) -> Dict[str, object]:
        """Watchdog state for /health (mirrors `BudgetGuard.status()`)."""
        return {'enabled': self._config.enabled,
                'stalled': sorted(self._stalled),
                'factor': self._config.factor,
                'floor_minutes': self._config.floor_minutes}

    def check(self, now: Optional[datetime] = None) -> List[StallEvent]:
        """Compare every worker against its threshold; return only the *transitions*.

        Pure apart from the episode memory it updates — no logging, no sending, no clock of its
        own when `now` is supplied. This is the seam the tests drive.
        """
        if not self._config.enabled:
            return []
        now = now or datetime.now(timezone.utc)
        events: List[StallEvent] = []
        for state in self._states_provider():
            threshold = self.threshold_seconds(state)
            # A worker that has never run yet is not stalled — it is starting up. Its first pass
            # fires immediately (the triggers run before their first sleep), so this window is
            # short and a stall after it is caught on the next tick.
            if state.last_run_at is None:
                continue
            silent = (now - state.last_run_at).total_seconds()
            is_stalled = silent > threshold
            was_stalled = state.name in self._stalled
            if is_stalled and not was_stalled:
                self._stalled.add(state.name)
                events.append(StallEvent(state.name, True, silent, threshold, state.last_run_at))
            elif not is_stalled and was_stalled:
                self._stalled.discard(state.name)
                events.append(StallEvent(state.name, False, silent, threshold, state.last_run_at))
        return events

    async def run(self) -> None:
        """Poll `check()` on the configured interval until stopped (started by the API lifespan)."""
        if not self._config.enabled:
            logger.info('[STALL] watchdog disabled by config')
            return
        self._stop.clear()
        logger.info('[STALL] watchdog armed — %dx cadence, floor %dm, checking every %ds',
                    self._config.factor, self._config.floor_minutes,
                    self._config.check_interval_seconds)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:   # noqa: BLE001 — a watchdog that dies is worse than one that errs
                logger.exception('[STALL] watchdog tick failed — next tick continues')
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self._config.check_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        self._stop.set()

    async def _tick(self) -> None:
        # The resource sample rides this tick (ISSUE_89) and is deliberately taken FIRST: it must
        # happen on every tick, not only on the ones that produce a stall event. `sample()` never
        # raises, and the caller's own `try` is the second belt.
        if self._gauge is not None:
            self._gauge.sample()
        events = self.check()
        if not events:
            return
        states = self._states_provider()
        total = len(states)
        running = total - len(self._stalled)
        for event in events:
            if event.stalled:
                logger.warning('[STALL] %s — no completed pass for %s (threshold %s); last ok %s',
                               event.worker, _format_age(event.silent_seconds),
                               _format_age(event.threshold_seconds),
                               event.last_run_at.isoformat() if event.last_run_at else 'never')
            else:
                logger.info('[STALL] %s recovered after %s',
                            event.worker, _format_age(event.silent_seconds))
            # The alert is best-effort by design: a failed send must never take the watchdog with
            # it — the log line above is already the durable record.
            if self._alert is not None:
                try:
                    await self._alert(event.message(running, total))
                except Exception:   # noqa: BLE001
                    logger.exception('[STALL] alert delivery failed for %s', event.worker)
