"""Builds and runs the background workers — the engine's live heartbeat (ISSUE_10)."""
import asyncio
import functools
import logging
from datetime import datetime, timezone
from typing import List, Optional

from finiexragengine.core.pipeline.breaking_bus import BreakingBus, BreakingSubscription
from finiexragengine.core.pipeline.eval_worker import EvalWorker
from finiexragengine.core.pipeline.ingest_worker import IngestWorker
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.triggers.event_trigger import EventTrigger
from finiexragengine.core.triggers.interval_trigger import IntervalTrigger
from finiexragengine.core.ui.engine_stats import EngineStats
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.alert_types import AlertCallback
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

    def __init__(self, assembler: PipelineAssembler, registry: PipelineRegistry,
                 pass_timeout_seconds: int = 300,
                 engine_stats: Optional[EngineStats] = None) -> None:
        # Optional (ISSUE_26): the live dashboard's shared state, injected into every worker so
        # each pass pushes its snapshot/events. None = no display (the default /health-only path).
        self._engine_stats = engine_stats
        # There is deliberately no shared lock here any more (ISSUE_74). One used to serialize
        # every pass — cheap at these cadences, and it kept the session-delta cost attribution
        # race-free — but it also meant a single blocked pass held all four workers hostage, which
        # is precisely how one silent RSS feed stopped the engine for nine days on 2026-08-01. The
        # two invariants it carried now live where they belong: cost accounting in
        # `CostRecorder.pass_scope()`, the breaking counters behind `EngineStats`' own lock. What
        # replaces it per worker is a deadline, not a lock — the triggers already guarantee a
        # worker cannot overlap itself.
        # The breaking wake bus (ISSUE_11): ingest workers publish flagged candidates, eval
        # workers subscribe per source-set with their own sensitivity — in-process, no infra.
        self._bus = BreakingBus()
        self._workers: List = []
        self._tasks: List[asyncio.Task] = []
        # True only between `stop_all()` and process exit — see `_worker_finished`.
        self._stopping: bool = False

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
                pass_timeout_seconds, cost_recorder=assembler.get_cost_recorder(),
                on_candidates=publish, engine_stats=engine_stats))

        for pipeline in registry.list_pipelines():
            config = pipeline.get_config()
            # Subscribe this stream to breaking wakes on its set, at its own sensitivity
            # (breaking.min_importance) — the filter that makes tiers per-pipeline (ISSUE_11).
            subscription = self._bus.subscribe(config.source_set, config.breaking.min_importance)
            self._workers.append(EvalWorker(
                pipeline,
                self._eval_trigger(config.trigger, subscription,
                                   f'pipeline {config.pipeline_id}'),
                pass_timeout_seconds, engine_stats=engine_stats,
                # Built here rather than inside the worker (ISSUE_82): the assembler owns the
                # per-pipeline graph and holds the outcome store the episode state is seeded from.
                episode_tracker=assembler.build_episode_tracker(config)))

    def set_host_alert(self, alert: Optional[AlertCallback]) -> None:
        """Route set-wide connectivity events to an alert channel (ISSUE_84).

        Only the ingest workers poll feeds, so only they can observe the condition. Wired here
        rather than at construction because the alert channel is built later (the same reason
        `StallWatchdog.set_alert` exists) — and, like the watchdog, detection and logging work
        with no channel at all.
        """
        for worker in self._workers:
            if isinstance(worker, IngestWorker):
                worker.set_host_alert(alert)

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
        self._stopping = False
        self._tasks = []
        for worker in self._workers:
            task = asyncio.create_task(worker.start(), name=worker.get_state().name)
            # A worker task that ends on its own is always a defect, and without this callback it
            # is a *silent* one: the exception stays parked in the Task, and because `self._tasks`
            # holds a strong reference the Task is never collected — so not even CPython's
            # "Task exception was never retrieved" is emitted. That is how the crypto ingest worker
            # died at 2026-08-20 19:24 UTC and was missed for 37 hours (ISSUE_82 follow-up).
            task.add_done_callback(functools.partial(self._worker_finished, worker.get_state()))
            self._tasks.append(task)

    def _worker_finished(self, state: WorkerState, task: asyncio.Task) -> None:
        """Record and shout about a worker task that ended while the engine was still running.

        Deliberately does not restart it. A worker that died from a code defect would die again on
        the same input, and a silent restart loop is the failure mode this is meant to end — being
        loud is the fix; reviving is a separate decision with its own issue.
        """
        if self._stopping or task.cancelled():
            return                                  # an orderly shutdown, not a death
        error = task.exception()
        state.stopped_at = datetime.now(timezone.utc)
        state.stopped_reason = (f'{type(error).__name__}: {error}' if error is not None
                                else 'the run loop returned without raising')
        if error is not None:
            logger.error('worker %s DIED — %s. It will not run again until a restart; '
                         'everything it feeds is now frozen', state.name, state.stopped_reason,
                         exc_info=error)
        else:
            logger.error('worker %s ENDED without an error — its run loop returned, which it '
                         'never should while the engine runs', state.name)

    async def stop_all(self) -> None:
        """Signal every trigger to stop, then wait for in-flight passes to finish."""
        # Tells `_worker_finished` that the tasks ending now are expected, not deaths.
        self._stopping = True
        for worker in self._workers:
            await worker.stop()
        for task in self._tasks:
            await task
        logger.info('workers stopped (%d)', len(self._tasks))
