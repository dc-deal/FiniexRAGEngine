"""Ingest worker — clocks one source-set's acquisition (ISSUE_10)."""
import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Dict, List, Optional, Set

from finiexragengine.core.observability.cost_recorder import CostRecorder
from finiexragengine.core.pipeline.ingestor import Ingestor
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger
from finiexragengine.core.ui.engine_stats import (
    EngineStats,
    IngestSnapshot,
    SourcesSnapshot,
)
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import IngestResult
from finiexragengine.types.worker_types import WorkerState

logger = logging.getLogger(__name__)

# A feed polled less than this many times its expected cadence reads as stuck, not merely slow.
_OVERDUE_FACTOR = 2.0


def _overdue_feeds(last_ok: Dict[str, datetime], expected: Dict[str, int],
                   now: datetime, skip: Set[str]) -> List[str]:
    """Feeds whose last successful poll is overdue vs their expected cadence — 'is it still alive?'.

    A healthy slow feed (its own `poll_interval_seconds`, politeness) cycles ok → floor_skip → ok,
    so its `last_ok` stays within its interval; only a feed that stopped polling for more than
    `_OVERDUE_FACTOR`× its expected gap is flagged. A feed already named this pass (quarantined /
    failed) is skipped to avoid a double marker; a feed never yet polled is normal at startup.
    """
    overdue: List[str] = []
    for source_id, interval in expected.items():
        if source_id in skip:
            continue
        last = last_ok.get(source_id)
        if last is None:
            continue
        overdue_s = (now - last).total_seconds()
        if overdue_s > interval * _OVERDUE_FACTOR:
            overdue.append(f'{source_id} overdue {int(overdue_s / 60)}m')
    return overdue


class IngestWorker:
    """Runs fetch -> embed-only-new -> upsert for ONE source-set on its own cadence.

    Cheap and time-critical by design: RSS windows slide, a missed article is gone
    forever — so this clocks faster than eval and never touches the LLM. One worker
    feeds every pipeline referencing the set (1x fetch, Nx read). A failing pass is
    logged and the loop continues — the corpus is append-only, the next tick heals.
    """

    def __init__(self, source_set: SourceSetConfig, ingestor: Ingestor,
                 trigger: AbstractTrigger, pass_lock: asyncio.Lock,
                 cost_recorder: Optional[CostRecorder] = None,
                 on_candidates: Optional[Callable[[int], None]] = None,
                 engine_stats: Optional[EngineStats] = None) -> None:
        self._ingestor = ingestor
        self._trigger = trigger
        # One lock across ALL workers: passes are seconds on minute cadences, so
        # serializing costs nothing — and it keeps the recorder's session-delta cost
        # attribution (runner envelopes, pass logs) race-free.
        self._pass_lock = pass_lock
        self._cost_recorder = cost_recorder
        # Optional (ISSUE_11): called with the highest importance tier flagged this pass, to
        # nudge the eval workers on this set out-of-band (the breaking bus). None = no wake.
        self._on_candidates = on_candidates
        # Optional (ISSUE_26): the live dashboard's shared state. None = no display (the
        # /health-only and CLI paths), in which case every push below is skipped — zero overhead.
        self._engine_stats = engine_stats
        # Per-feed expected cadence (its own poll_interval / politeness, else the set's interval)
        # + the last successful poll, so a stuck slow feed can be flagged overdue on the dashboard.
        set_interval = source_set.trigger.interval_seconds
        self._expected: Dict[str, int] = {
            source.source_id: (source.poll_interval_seconds or set_interval)
            for source in source_set.active_sources()}
        self._last_ok: Dict[str, datetime] = {}
        self._state = WorkerState(name=f'ingest:{source_set.source_set_id}',
                                  kind='ingest',
                                  interval_seconds=source_set.trigger.interval_seconds)

    def get_state(self) -> WorkerState:
        return self._state

    async def start(self) -> None:
        await self._trigger.start(self._pass)

    async def stop(self) -> None:
        await self._trigger.stop()

    async def _pass(self) -> None:
        async with self._pass_lock:
            started = perf_counter()
            usd_before = self._cost_recorder.session_usd if self._cost_recorder else 0.0
            self._state.last_run_at = datetime.now(timezone.utc)
            try:
                # The pass body is synchronous (feeds, OpenAI, psycopg) — run it in a
                # thread so the event loop keeps serving the API while we work.
                result = await asyncio.to_thread(self._ingestor.run)
            except Exception as exc:   # noqa: BLE001 — a pass must never kill the loop
                self._state.last_status = 'error'
                self._state.last_detail = str(exc)
                logger.exception('[%s] pass failed — next tick continues', self._state.name)
            else:
                usd = (self._cost_recorder.session_usd - usd_before
                       if self._cost_recorder else 0.0)
                self._state.last_status = 'ok'
                # Prefix a suspended pass (provider quota, ISSUE_47) so it is visible, not silent.
                prefix = 'suspended (quota) · ' if result.suspended else ''
                self._state.last_detail = (f'{prefix}fetched {result.fetched} · '
                                           f'embedded {result.embedded} · '
                                           f'stored {result.stored}')
                # Surface breaking candidates in the pass line when any were flagged (ISSUE_11).
                if result.candidates:
                    self._state.last_detail += f' · flagged {result.candidates} breaking'
                # Sources the pass did not poll ride along on the pass line rather than getting
                # their own log entries: a quarantine lasts hours, so on a 15s cadence a per-skip
                # line would emit thousands of identical repeats. Here the count is visible on a
                # line that prints anyway — and on the worker state the API serves.
                if result.quarantined_skips:
                    self._state.last_detail += (f' · {len(result.quarantined_skips)} quarantined '
                                                f'({", ".join(result.quarantined_skips)})')
                # A quiet pass (nothing new, nothing flagged, $0 — the common case once the
                # corpus is warm and conditional GET is 304ing) logs at DEBUG so an overnight
                # run's log stays readable; a pass that stored, flagged or spent logs at INFO —
                # so spend is still never silent (a paid pass always has stored > 0). The eval
                # workers' INFO passes remain the regular liveness heartbeat either way.
                eventful = (result.stored or result.candidates or usd
                            or result.failed_sources or result.suspended)
                duration_ms = (perf_counter() - started) * 1000.0
                logger.log(logging.INFO if eventful else logging.DEBUG,
                           '[%s] %s · $%.6f · %.0fms', self._state.name,
                           self._state.last_detail, usd, duration_ms)
                self._log_source_health(result)
                # Feed the live dashboard from the same structured pass (ISSUE_26) — next to the
                # log call, never parsed back from it. Skipped entirely without a display.
                self._push_stats(result, usd, duration_ms, eventful)
                # Nudge the eval workers on this set out-of-band (ISSUE_11) — in the event
                # loop thread, after the sync pass returned. A missed nudge is harmless: the
                # candidate is already persisted, the eval worker still catches it next interval.
                if self._on_candidates is not None and result.max_tier > 0:
                    self._on_candidates(result.max_tier)
            self._state.runs += 1
            self._state.last_duration_ms = (perf_counter() - started) * 1000.0

    def _push_stats(self, result: IngestResult, usd: float, duration_ms: float,
                    eventful: bool) -> None:
        """Push this pass into the live dashboard's shared state (ISSUE_26); a no-op without one."""
        stats = self._engine_stats
        if stats is None:
            return
        now = datetime.now(timezone.utc)
        source_set_id = self._set_name()          # this worker's key — one SOURCES/INGEST row per set
        # Track each feed's last successful poll, then flag one that stopped polling vs its expected
        # cadence — 'is my slow (politeness) feed still alive?' A healthy slow feed cycles within its
        # interval and is not flagged; a quarantined/failed feed is already named, so it is skipped.
        for poll in result.polls:
            if poll.status == 'ok':
                self._last_ok[poll.source_id] = now
        already: Set[str] = set(result.quarantined_skips) | set(result.failed_sources)
        # SOURCES row: healthy collapses to `N/N ok`; only failed/quarantined/overdue feeds named.
        ok = sum(1 for poll in result.polls if poll.status == 'ok')
        deviations = ([f'{source_id} quarantined' for source_id in result.quarantined_skips]
                      + [f'{source_id} failed' for source_id in result.failed_sources]
                      + _overdue_feeds(self._last_ok, self._expected, now, already))
        stats.set_sources(source_set_id, SourcesSnapshot(last=now, ok=ok, total=len(result.polls),
                                                         deviations=deviations))
        # INGEST row + the activity line (only an eventful pass streams — mirrors the log level so
        # a warm 304-ing corpus does not flood the stream).
        stats.set_ingest(source_set_id, IngestSnapshot(last=now, fetched=result.fetched,
                                                       new=result.stored, cost_usd=usd,
                                                       duration_ms=duration_ms,
                                                       suspended=result.suspended))
        if eventful:
            stats.push_event('INGEST', f'{source_set_id} {self._state.last_detail}')
        if result.suspended:
            stats.push_event('BUDGET', 'embedding suspended — provider quota')
        # BREAKING (detected side): cumulative HIGH-tier candidates flagged by ingest (ISSUE_11).
        if result.candidates:
            stats.add_breaking_detected(result.candidates, at=now)
        # A feed crossing into flagged+quarantined this pass gets its own red activity line.
        for source_id, note in result.health_notes.items():
            if note.just_flagged:
                stats.push_event('SOURCE', f'{source_id} flagged + quarantined')

    def _set_name(self) -> str:
        # 'ingest:crypto_news' -> 'crypto_news' for the compact stream line.
        return self._state.name.split(':', 1)[-1]

    def _log_source_health(self, result: IngestResult) -> None:
        """Emit source-failure lines at a level that denoises repeats (ISSUE_11).

        A feed that fails every pass (e.g. cryptoslate rate-limiting a fast loop) would otherwise
        flood the log. So: WARN the first failure of a streak, DEBUG the repeats, WARN once when it
        crosses into flagged+quarantined, and INFO a recovery. The full detail always persists in
        source_health regardless of the console level — the Sources report reads it from there."""
        for source_id in result.recovered_sources:
            logger.info('[%s] source %s recovered', self._state.name, source_id)
        # A skipped source is traceable at DEBUG only: entering quarantine already WARNed once
        # (`just_flagged` below), and the steady state is carried by the pass line + the Sources
        # report. Repeating it per pass would drown the signal it is meant to raise.
        for source_id in result.quarantined_skips:
            logger.debug('[%s] source %s skipped — quarantined', self._state.name, source_id)
        for source_id, message in result.failed_sources.items():
            note = result.health_notes.get(source_id)
            if note is not None and note.just_flagged:
                logger.warning('[%s] source %s flagged + quarantined until %s (%d consecutive): %s',
                               self._state.name, source_id,
                               note.quarantined_until.isoformat() if note.quarantined_until else '?',
                               note.consecutive_failures, message)
            elif note is None or note.consecutive_failures <= 1:
                logger.warning('[%s] source %s failed: %s', self._state.name, source_id, message)
            else:
                logger.debug('[%s] source %s still failing (%dx): %s', self._state.name,
                             source_id, note.consecutive_failures, message)
