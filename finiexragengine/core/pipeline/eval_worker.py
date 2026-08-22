"""Eval worker — clocks one logical pipeline's evaluation (ISSUE_10)."""
import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Optional

from finiexragengine.core.pipeline.breaking_episode import (
    BreakingEpisode,
    BreakingEpisodeTracker,
    BreakingPass,
)
from finiexragengine.core.pipeline.pipeline import Pipeline
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger
from finiexragengine.core.ui.engine_stats import (
    EngineStats,
    LlmSnapshot,
    RetrievalSnapshot,
)
from finiexragengine.types.outcome_types import AnalysisEnvelope
from finiexragengine.types.trigger_types import TriggerReason
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
                 pass_timeout_seconds: int = 300,
                 engine_stats: Optional[EngineStats] = None,
                 episode_tracker: Optional[BreakingEpisodeTracker] = None) -> None:
        self._pipeline = pipeline
        self._trigger = trigger
        # Wall-clock deadline for one pass (ISSUE_74) — see IngestWorker for the rationale, and
        # for why no lock replaced the shared one that used to sit here.
        self._pass_timeout_seconds = pass_timeout_seconds
        # Optional (ISSUE_26): the live dashboard's shared state. None = no display — every
        # push below is skipped, so the /health-only and CLI paths carry zero overhead.
        self._engine_stats = engine_stats
        # Edge-triggered breaking (ISSUE_11): a hot story is counted/logged once, on the transition
        # into breaking — not every pass it lingers. Built and seeded by the assembler (ISSUE_82),
        # which has both the pipeline's `breaking` config and the outcome store; the bare fallback
        # keeps direct construction (tests, /run) working on the schema defaults.
        self._episodes = episode_tracker if episode_tracker is not None else BreakingEpisodeTracker()
        config = pipeline.get_config()
        # Eval cadence is a bar-close timeframe (ISSUE_timeframe); expose it as the label plus
        # the derived seconds value (via cadence_seconds) so /health still shows a number.
        self._state = WorkerState(name=f'eval:{config.pipeline_id}', kind='eval',
                                  interval_seconds=config.trigger.cadence_seconds,
                                  timeframe=config.trigger.timeframe)
        # A story running across a restart stays on the dashboard (ISSUE_82). The assembler seeded
        # the rule from the store, so the episodes are known — without this the panel showed
        # `none active` for up to a full gap while one was demonstrably open.
        self._restore_open_episodes()

    def _restore_open_episodes(self) -> None:
        """Show the episodes this process inherited; the session counters stay untouched."""
        stats = self._engine_stats
        if stats is None:
            return
        gap_seconds = self._episodes.get_rule().get_gap().total_seconds()
        for running in self._episodes.open_episodes():
            # No reaction time: the replay re-opened this episode at the window's edge, so any
            # measurement here would be re-sampled against stale evidence (see the store method).
            stats.restore_breaking_episode(running.episode.symbol, running.episode.signal,
                                           running.episode.reason, started=running.started,
                                           last_seen=running.last_seen, gap_seconds=gap_seconds,
                                           started_bounded=running.started_bounded)

    def get_state(self) -> WorkerState:
        return self._state

    async def start(self) -> None:
        await self._trigger.start(self._pass)

    async def stop(self) -> None:
        await self._trigger.stop()

    async def _pass(self, reason: TriggerReason) -> None:
        started = perf_counter()
        self._state.last_run_at = datetime.now(timezone.utc)
        try:
            # No shared lock any more (ISSUE_74) — this pass runs alongside the others instead of
            # queueing behind them. The deadline abandons the await, not the thread, so the worker
            # recovers on its next bar close rather than staying dead until a restart. The run's
            # own cost scope lives in `PipelineRunner.run`, where the envelope is assembled.
            envelope = await asyncio.wait_for(asyncio.to_thread(self._pipeline.run, reason),
                                              timeout=self._pass_timeout_seconds)
        # Every branch opens its detail with the pass's reason (ISSUE_87) — `last_detail` is the one
        # string the log line, the live activity stream and /health all render, so writing it here
        # (rather than only into the envelope) puts the reason into the visible history too.
        except asyncio.TimeoutError:
            self._state.last_status = 'error'
            self._state.last_detail = (f'{reason} · pass exceeded '
                                       f'{self._pass_timeout_seconds}s deadline')
            logger.warning('[%s] %s pass exceeded %ds deadline — abandoned, next tick continues',
                           self._state.name, reason, self._pass_timeout_seconds)
        except Exception as exc:   # noqa: BLE001 — a pass must never kill the loop
            self._state.last_status = 'error'
            self._state.last_detail = f'{reason} · {exc}'
            logger.exception('[%s] %s pass failed — next tick continues',
                             self._state.name, reason)
        else:
            m = envelope.metadata
            llm_rows = sum(1 for r in envelope.result if r.basis == 'llm')
            self._state.last_status = 'ok' if envelope.status != 'error' else 'error'
            self._state.last_detail = (f'{reason} · {envelope.status} · '
                                       f'{len(envelope.result)} symbols '
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
            #
            # Guarded like the ingest twin: the envelope is persisted by now, so nothing here can
            # be worth losing the worker over. This block used to sit outside every handler, which
            # is how one unexercised code path cost 37 hours of crypto ingest on 2026-08-20.
            try:
                breaking = self._episodes.observe(envelope)
                for episode in breaking.started:
                    logger.info(_breaking_line(envelope.pipeline_id, episode))
                # Feed the live dashboard from the same envelope (ISSUE_26); no-op without one.
                self._push_stats(envelope, tokens, duration_ms, breaking)
            except Exception:   # noqa: BLE001 — reporting must never end the worker
                logger.exception('[%s] pass reporting failed — the envelope is persisted '
                                 'and the worker continues', self._state.name)
        self._state.runs += 1
        self._state.last_duration_ms = (perf_counter() - started) * 1000.0

    def _push_stats(self, envelope: AnalysisEnvelope, tokens: int, duration_ms: float,
                    breaking: BreakingPass) -> None:
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
        # signals carry base_currency (chip label) + the retrieval query (the analysis-unit key), so
        # the display merges ONLY genuinely-fanned same-query symbols — ETHUSD/ETHEUR, never a
        # same-base different-query pair like USDJPY/USDCAD (ISSUE_70). `calls` = analysis units.
        qmap = self._pipeline.get_config().symbol_query_map()
        stats.set_llm(pipeline_id, LlmSnapshot(
            last=now, tokens=tokens, cost_usd=m.cost_usd, duration_ms=duration_ms,
            signals=[(r.symbol, r.signal, r.base_currency or '', qmap.get(r.symbol, r.symbol))
                     for r in envelope.result],
            calls=len(m.per_symbol_tokens)))
        stats.push_event('LLM', f'{pipeline_id} {self._state.last_detail}')
        # BREAKING (confirmed side): one activity line + one recorded episode per NEW episode —
        # bumps the count, sets the frozen reaction detail, and feeds the BREAKING section with the
        # episode's reason (ISSUE_64).
        gap_seconds = self._episodes.get_rule().get_gap().total_seconds()
        for episode in breaking.started:
            stats.push_event('BREAKING', _breaking_line(envelope.pipeline_id, episode))
            detail = (f'engine {_fmt_seconds(episode.engine_s)} / '
                      f'e2e {_fmt_seconds(episode.end_to_end_s)}')
            # The record carries its pipeline's gap so the renderer decides live-vs-ended against
            # the value the rule actually used (ISSUE_82) — `breaking` is per-pipeline config.
            stats.add_breaking_episode(episode.symbol, episode.signal, episode.reason, detail,
                                       at=now, gap_seconds=gap_seconds)
        # A symbol whose open episode this pass HELD is an ongoing story: advance its record's
        # last_seen so the section keeps it 'live' and grows its duration (ISSUE_64). Under
        # hysteresis this is no longer "was breaking" — a pass below the confirm gate but at or
        # above the exit gate holds the story open too (ISSUE_82).
        for symbol in breaking.held:
            stats.touch_breaking_episode(symbol, at=now)
