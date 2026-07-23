"""Eval worker — clocks one logical pipeline's evaluation (ISSUE_10)."""
import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import List, Optional

from finiexragengine.core.pipeline.breaking_episode import (
    BreakingEpisode,
    BreakingEpisodeTracker,
)
from finiexragengine.core.pipeline.pipeline import Pipeline
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger
from finiexragengine.core.ui.engine_stats import (
    EngineStats,
    LlmSnapshot,
    RetrievalSnapshot,
)
from finiexragengine.types.outcome_types import AnalysisEnvelope
from finiexragengine.types.worker_types import WorkerState

logger = logging.getLogger(__name__)


def _fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return '—'
    return f'{seconds:.0f}s' if seconds < 90 else f'{seconds / 60:.1f}m'


def _breaking_line(pipeline_id: str, episode: BreakingEpisode) -> str:
    """The `[BREAKING ✓]` log/stream line for one episode start, with its frozen reaction time.

    Engine reaction = envelope timestamp − earliest source `fetched_at` (what we control);
    end-to-end = − earliest REAL `published_at` (estimated dates excluded). Anchored once at the
    episode start (see `breaking_episode`), so it matches the store-based report by construction.
    """
    return (f'[BREAKING ✓] {pipeline_id} {episode.symbol} {episode.signal} '
            f'urgency {episode.urgency:.2f} · engine {_fmt_seconds(episode.engine_s)} / '
            f'e2e {_fmt_seconds(episode.end_to_end_s)} · {episode.n_sources} sources')


class EvalWorker:
    """Runs retrieve -> LLM -> assemble -> persist for ONE logical pipeline.

    One worker per stream — fan-out variants (ISSUE_42) each get their own, so
    double-tracking runs automatically. The pipeline's runner is ingest-less in
    worker mode (the ingest worker owns acquisition); it persists its envelope
    itself (ISSUE_8), so a pass leaves nothing to hand over. The envelope contract
    absorbs stage failures; anything residual is logged and the loop continues.
    """

    def __init__(self, pipeline: Pipeline, trigger: AbstractTrigger,
                 pass_lock: asyncio.Lock,
                 engine_stats: Optional[EngineStats] = None) -> None:
        self._pipeline = pipeline
        self._trigger = trigger
        # Shared across all workers — see IngestWorker: keeps session-delta cost
        # attribution race-free; serialization is free at these cadences.
        self._pass_lock = pass_lock
        # Optional (ISSUE_26): the live dashboard's shared state. None = no display — every
        # push below is skipped, so the /health-only and CLI paths carry zero overhead.
        self._engine_stats = engine_stats
        # Edge-triggered breaking (ISSUE_11): a hot story is counted/logged once, on the transition
        # into breaking — not every pass it lingers. Session-scoped; the store report is durable.
        self._episodes = BreakingEpisodeTracker()
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
                duration_ms = (perf_counter() - started) * 1000.0
                tokens = m.prompt_tokens + m.completion_tokens
                # Spend is never silent: tokens + USD per pass, right where it runs.
                logger.info('[%s] %s · %d tok · $%.6f · %.0fms → outcomes',
                            self._state.name, self._state.last_detail,
                            tokens, m.cost_usd, duration_ms)
                # Confirmed breaking, edge-triggered (ISSUE_11): a hot story is logged once, on the
                # transition into breaking — not every pass it lingers (that flooded the log with 59
                # identical lines/day and inflated the count). Cross-checks the store `breaking`
                # report, which groups the same episodes.
                episodes = self._episodes.new_episodes(envelope)
                for episode in episodes:
                    logger.info(_breaking_line(envelope.pipeline_id, episode))
                # Feed the live dashboard from the same envelope (ISSUE_26); no-op without a display.
                self._push_stats(envelope, tokens, duration_ms, episodes)
            self._state.runs += 1
            self._state.last_duration_ms = (perf_counter() - started) * 1000.0

    def _push_stats(self, envelope: AnalysisEnvelope, tokens: int, duration_ms: float,
                    episodes: List[BreakingEpisode]) -> None:
        """Push this eval pass into the live dashboard's shared state (ISSUE_26); no-op without one."""
        stats = self._engine_stats
        if stats is None:
            return
        now = datetime.now(timezone.utc)
        m = envelope.metadata
        pipeline_id = envelope.pipeline_id        # this worker's key — one RETRIEVAL/LLM row per pipeline
        # RETRIEVAL folds off the eval pass (no clock of its own): what the LLM actually read.
        stats.set_retrieval(pipeline_id, RetrievalSnapshot(last=now, retrieved=m.articles_relevant,
                                                           symbols=len(envelope.result)))
        # LLM row: spend + one signal per symbol, in symbol order (a single arrow would lie).
        stats.set_llm(pipeline_id, LlmSnapshot(
            last=now, tokens=tokens, cost_usd=m.cost_usd, duration_ms=duration_ms,
            signals=[(r.symbol, r.signal) for r in envelope.result]))
        stats.push_event('LLM', f'{pipeline_id} {self._state.last_detail}')
        # BREAKING (confirmed side): one activity line + one recorded episode per NEW episode —
        # bumps the count, sets the frozen reaction detail, and feeds the BREAKING section with the
        # episode's reason (ISSUE_64).
        started_symbols = {episode.symbol for episode in episodes}
        for episode in episodes:
            stats.push_event('BREAKING', _breaking_line(envelope.pipeline_id, episode))
            detail = (f'engine {_fmt_seconds(episode.engine_s)} / '
                      f'e2e {_fmt_seconds(episode.end_to_end_s)}')
            stats.add_breaking_episode(episode.symbol, episode.signal, episode.reason, detail, at=now)
        # A symbol still breaking but NOT a new episode is an ongoing story (edge-triggered): advance
        # its record's last_seen so the section keeps it 'live' and grows its duration (ISSUE_64).
        for result in envelope.result:
            if result.is_breaking and result.symbol not in started_symbols:
                stats.touch_breaking_episode(result.symbol, at=now)
