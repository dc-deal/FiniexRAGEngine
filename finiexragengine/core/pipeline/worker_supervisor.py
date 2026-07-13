"""Builds and runs the background workers — the engine's live heartbeat (ISSUE_10)."""
import asyncio
import logging
from typing import List

from finiexragengine.core.pipeline.eval_worker import EvalWorker
from finiexragengine.core.pipeline.ingest_worker import IngestWorker
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.triggers.interval_trigger import IntervalTrigger
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.worker_types import WorkerState

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
        self._workers: List = []
        self._tasks: List[asyncio.Task] = []

        # One ingest worker per source-set actually referenced by a pipeline — a set
        # nobody evaluates over would only burn embedding budget.
        referenced = {p.get_config().source_set for p in registry.list_pipelines()}
        for source_set_id in sorted(referenced):
            source_set = assembler.get_source_sets().get(source_set_id)
            self._workers.append(IngestWorker(
                source_set, assembler.build_ingestor(source_set_id),
                self._interval_trigger(source_set.trigger, f'source-set {source_set_id}'),
                pass_lock, cost_recorder=assembler.get_cost_recorder()))

        for pipeline in registry.list_pipelines():
            config = pipeline.get_config()
            self._workers.append(EvalWorker(
                pipeline,
                self._interval_trigger(config.trigger, f'pipeline {config.pipeline_id}'),
                pass_lock))

    @staticmethod
    def _interval_trigger(trigger_config, owner: str) -> IntervalTrigger:
        # Only the interval trigger exists today; the event trigger arrives with the
        # breaking path (ISSUE_11) and will plug into the same start/stop contract.
        if trigger_config.type != 'interval':
            raise ConfigurationError(
                f"unsupported trigger type '{trigger_config.type}' on {owner} — "
                "only 'interval' is implemented (event triggers land with ISSUE_11)")
        return IntervalTrigger(trigger_config.interval_seconds)

    def states(self) -> List[WorkerState]:
        return [worker.get_state() for worker in self._workers]

    async def start_all(self) -> None:
        """Launch every worker as its own task; returns immediately."""
        for state in self.states():
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
