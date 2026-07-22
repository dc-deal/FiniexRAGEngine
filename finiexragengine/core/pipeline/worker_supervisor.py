"""Builds and runs the background workers — the engine's live heartbeat (ISSUE_10)."""
import asyncio
import functools
import logging
from datetime import datetime, timezone
from typing import List

from finiexragengine.core.pipeline.breaking_bus import BreakingBus, BreakingSubscription
from finiexragengine.core.pipeline.eval_worker import EvalWorker
from finiexragengine.core.pipeline.ingest_worker import IngestWorker
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.triggers.event_trigger import EventTrigger
from finiexragengine.core.triggers.interval_trigger import IntervalTrigger
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.pipeline_config_types import TriggerConfig
from finiexragengine.types.worker_types import WorkerState
from finiexragengine.utils.timeframe import TIMEFRAMES, seconds_until_next_boundary

logger = logging.getLogger(__name__)


class WorkerSupervisor:
    """Owns the two-worker model: N ingest workers (one per *referenced* source-set)
    and M eval workers (one per logical pipeline, fan variants included), each on its
    own cadence over the one shared corpus. Built from the assembler's wiring; started
    and stopped by the API lifespan (opt-in `--workers` — paid background activity is
    a deliberate choice).
    """

    def __init__(self, assembler: PipelineAssembler, registry: PipelineRegistry) -> None:
        # One lock across every pass — see IngestWorker for the attribution rationale.
        pass_lock = asyncio.Lock()
        # The breaking wake bus (ISSUE_11): ingest workers publish flagged candidates, eval
        # workers subscribe per source-set with their own sensitivity — in-process, no infra.
        self._bus = BreakingBus()
        self._workers: List = []
        self._tasks: List[asyncio.Task] = []

        # One ingest worker per source-set actually referenced by a pipeline — a set
        # nobody evaluates over would only burn embedding budget.
        referenced = {p.get_config().source_set for p in registry.list_pipelines()}
        for source_set_id in sorted(referenced):
            source_set = assembler.get_source_sets().get(source_set_id)
            # Bind this set's publish so the ingest worker can nudge its eval workers (ISSUE_11).
            publish = functools.partial(self._bus.publish, source_set_id)
            self._workers.append(IngestWorker(
                source_set, assembler.build_ingestor(source_set_id),
                self._interval_trigger(source_set.trigger, f'source-set {source_set_id}'),
                pass_lock, cost_recorder=assembler.get_cost_recorder(),
                on_candidates=publish))

        for pipeline in registry.list_pipelines():
            config = pipeline.get_config()
            # Subscribe this stream to breaking wakes on its set, at its own sensitivity
            # (breaking.min_importance) — the filter that makes tiers per-pipeline (ISSUE_11).
            subscription = self._bus.subscribe(config.source_set, config.breaking.min_importance)
            self._workers.append(EvalWorker(
                pipeline,
                self._eval_trigger(config.trigger, subscription,
                                   f'pipeline {config.pipeline_id}'),
                pass_lock))

    @staticmethod
    def _interval_trigger(trigger_config: TriggerConfig, owner: str) -> IntervalTrigger:
        # Ingest workers run on a pure interval; the breaking path drives eval, not ingest.
        if trigger_config.type != 'interval':
            raise ConfigurationError(
                f"unsupported trigger type '{trigger_config.type}' on {owner} — "
                "only 'interval' is implemented for ingest")
        return IntervalTrigger(trigger_config.interval_seconds)

    @staticmethod
    def _eval_trigger(trigger_config: TriggerConfig, subscription: BreakingSubscription,
                      owner: str) -> EventTrigger:
        # Eval workers fire on their bar-close grid AND jump the queue on a breaking wake
        # (ISSUE_11 + ISSUE_timeframe). The wait is recomputed each cycle from the live clock,
        # so the grid stays exact regardless of boot time or pass duration.
        if trigger_config.type != 'interval':
            raise ConfigurationError(
                f"unsupported trigger type '{trigger_config.type}' on {owner} — "
                "only 'interval' is implemented")
        timeframe = trigger_config.timeframe
        if timeframe is None:
            raise ConfigurationError(
                f'eval trigger on {owner} needs a `timeframe` (bar-close cadence) — '
                f'one of {", ".join(TIMEFRAMES)}')
        return EventTrigger(
            lambda: seconds_until_next_boundary(datetime.now(timezone.utc), timeframe),
            subscription)

    def states(self) -> List[WorkerState]:
        return [worker.get_state() for worker in self._workers]

    async def start_all(self) -> None:
        """Launch every worker as its own task; returns immediately."""
        for state in self.states():
            # Eval workers announce their bar-close frame; ingest workers their raw interval.
            if state.timeframe is not None:
                logger.info('worker %s on %s (bar-close, %ds grid)',
                            state.name, state.timeframe, state.interval_seconds)
            else:
                logger.info('worker %s every %ds', state.name, state.interval_seconds)
        self._tasks = [asyncio.create_task(worker.start(), name=worker.get_state().name)
                       for worker in self._workers]

    async def stop_all(self) -> None:
        """Signal every trigger to stop, then wait for in-flight passes to finish."""
        for worker in self._workers:
            await worker.stop()
        for task in self._tasks:
            await task
        logger.info('workers stopped (%d)', len(self._tasks))
