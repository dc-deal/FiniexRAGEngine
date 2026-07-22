"""Eval worker — clocks one logical pipeline's evaluation (ISSUE_10)."""
import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import List, Optional

from finiexragengine.core.pipeline.pipeline import Pipeline
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger
from finiexragengine.types.outcome_types import AnalysisEnvelope
from finiexragengine.types.worker_types import WorkerState

logger = logging.getLogger(__name__)


def _fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return '—'
    return f'{seconds:.0f}s' if seconds < 90 else f'{seconds / 60:.1f}m'


def _breaking_confirmations(envelope: AnalysisEnvelope) -> List[str]:
    """One `[BREAKING ✓]` line per confirmed breaking result, with its reaction time (ISSUE_11).

    Engine reaction = envelope timestamp − earliest source `fetched_at` (what we control);
    end-to-end = − earliest `published_at` (what the consumer feels). Both from the envelope,
    so it matches the store-based report exactly.
    """
    lines = []
    for result in envelope.result:
        if not result.is_breaking:
            continue
        fetched = [s.fetched_at for s in result.sources if s.fetched_at]
        published = [s.published_at for s in result.sources if s.published_at]
        engine = (envelope.timestamp - min(fetched)).total_seconds() if fetched else None
        end_to_end = (envelope.timestamp - min(published)).total_seconds() if published else None
        lines.append(
            f'[BREAKING ✓] {envelope.pipeline_id} {result.symbol} {result.signal} '
            f'urgency {result.urgency:.2f} · engine {_fmt_seconds(engine)} / '
            f'e2e {_fmt_seconds(end_to_end)} · {len(result.sources)} sources')
    return lines


class EvalWorker:
    """Runs retrieve -> LLM -> assemble -> persist for ONE logical pipeline.

    One worker per stream — fan-out variants (ISSUE_42) each get their own, so
    double-tracking runs automatically. The pipeline's runner is ingest-less in
    worker mode (the ingest worker owns acquisition); it persists its envelope
    itself (ISSUE_8), so a pass leaves nothing to hand over. The envelope contract
    absorbs stage failures; anything residual is logged and the loop continues.
    """

    def __init__(self, pipeline: Pipeline, trigger: AbstractTrigger,
                 pass_lock: asyncio.Lock) -> None:
        self._pipeline = pipeline
        self._trigger = trigger
        # Shared across all workers — see IngestWorker: keeps session-delta cost
        # attribution race-free; serialization is free at these cadences.
        self._pass_lock = pass_lock
        config = pipeline.get_config()
        # Eval cadence is a bar-close timeframe (ISSUE_timeframe); expose it as the label plus
        # the derived seconds value (via cadence_seconds) so /health still shows a number.
        self._state = WorkerState(name=f'eval:{config.pipeline_id}', kind='eval',
                                  interval_seconds=config.trigger.cadence_seconds,
                                  timeframe=config.trigger.timeframe)

    def get_state(self) -> WorkerState:
        return self._state

    async def start(self) -> None:
        await self._trigger.start(self._pass)

    async def stop(self) -> None:
        await self._trigger.stop()

    async def _pass(self) -> None:
        async with self._pass_lock:
            started = perf_counter()
            self._state.last_run_at = datetime.now(timezone.utc)
            try:
                envelope = await asyncio.to_thread(self._pipeline.run)
            except Exception as exc:   # noqa: BLE001 — a pass must never kill the loop
                self._state.last_status = 'error'
                self._state.last_detail = str(exc)
                logger.exception('[%s] pass failed — next tick continues', self._state.name)
            else:
                m = envelope.metadata
                llm_rows = sum(1 for r in envelope.result if r.basis == 'llm')
                self._state.last_status = 'ok' if envelope.status != 'error' else 'error'
                self._state.last_detail = (f'{envelope.status} · {len(envelope.result)} symbols '
                                           f'({llm_rows} llm · {len(envelope.result) - llm_rows} other)')
                # Spend is never silent: tokens + USD per pass, right where it runs.
                logger.info('[%s] %s · %d tok · $%.6f · %.0fms → outcomes',
                            self._state.name, self._state.last_detail,
                            m.prompt_tokens + m.completion_tokens, m.cost_usd,
                            (perf_counter() - started) * 1000.0)
                # Per-breaking reaction time, logged the moment it is confirmed (ISSUE_11) — so an
                # overnight run is self-documenting: every confirmed breaking shows its latency
                # inline, and it cross-checks the store-based `breaking` report.
                for line in _breaking_confirmations(envelope):
                    logger.info(line)
            self._state.runs += 1
            self._state.last_duration_ms = (perf_counter() - started) * 1000.0
