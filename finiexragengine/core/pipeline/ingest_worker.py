"""Ingest worker — clocks one source-set's acquisition (ISSUE_10)."""
import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, Optional

from finiexragengine.core.pipeline.ingestor import Ingestor
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.worker_types import WorkerState

logger = logging.getLogger(__name__)


class IngestWorker:
    """Runs fetch -> embed-only-new -> upsert for ONE source-set on its own cadence.

    Cheap and time-critical by design: RSS windows slide, a missed article is gone
    forever — so this clocks faster than eval and never touches the LLM. One worker
    feeds every pipeline referencing the set (1x fetch, Nx read). A failing pass is
    logged and the loop continues — the corpus is append-only, the next tick heals.
    """

    def __init__(self, source_set: SourceSetConfig, ingestor: Ingestor,
                 trigger: AbstractTrigger, pass_lock: asyncio.Lock,
                 cost_recorder=None,
                 on_candidates: Optional[Callable[[int], None]] = None) -> None:
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
                self._state.last_detail = (f'fetched {result.fetched} · '
                                           f'embedded {result.embedded} · '
                                           f'stored {result.stored}')
                # Surface breaking candidates in the pass line when any were flagged (ISSUE_11).
                if result.candidates:
                    self._state.last_detail += f' · flagged {result.candidates} breaking'
                # A quiet pass (nothing new, nothing flagged, $0 — the common case once the
                # corpus is warm and conditional GET is 304ing) logs at DEBUG so an overnight
                # run's log stays readable; a pass that stored, flagged or spent logs at INFO —
                # so spend is still never silent (a paid pass always has stored > 0). The eval
                # workers' INFO passes remain the regular liveness heartbeat either way.
                eventful = result.stored or result.candidates or usd or result.failed_sources
                logger.log(logging.INFO if eventful else logging.DEBUG,
                           '[%s] %s · $%.6f · %.0fms', self._state.name,
                           self._state.last_detail, usd,
                           (perf_counter() - started) * 1000.0)
                for source_id, message in result.failed_sources.items():
                    logger.warning('[%s] source %s failed: %s', self._state.name,
                                   source_id, message)
                # Nudge the eval workers on this set out-of-band (ISSUE_11) — in the event
                # loop thread, after the sync pass returned. A missed nudge is harmless: the
                # candidate is already persisted, the eval worker still catches it next interval.
                if self._on_candidates is not None and result.max_tier > 0:
                    self._on_candidates(result.max_tier)
            self._state.runs += 1
            self._state.last_duration_ms = (perf_counter() - started) * 1000.0
