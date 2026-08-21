"""Health + pipeline-listing endpoints."""
from typing import Optional

from fastapi import APIRouter

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.core.observability.stall_watchdog import StallWatchdog
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor
from finiexragengine.utils.timeframe import timeframe_minutes
from finiexragengine.types.api_types import (
    BudgetInfo,
    HealthResponse,
    PipelineInfo,
    PipelinesResponse,
    ResourceInfo,
    StallInfo,
    WorkerInfo,
)


def _cadence_seconds(timeframe: Optional[str]) -> Optional[int]:
    """The trigger's timeframe as seconds — None when it carries none.

    An unknown token would raise here rather than on the worker's first tick, which is the wrong
    place to find out: the listing must stay answerable even when a pipeline is misconfigured, so
    an unparseable timeframe reports as absent and the configuration error surfaces where it is
    acted on (the supervisor refuses to schedule it).
    """
    if timeframe is None:
        return None
    try:
        return timeframe_minutes(timeframe) * 60
    except ValueError:
        return None


def build_health_router(config_manager: AppConfigManager,
                        registry: PipelineRegistry,
                        supervisor: Optional[WorkerSupervisor] = None,
                        budget_guard: Optional[BudgetGuard] = None,
                        stall_watchdog: Optional[StallWatchdog] = None,
                        resource_gauge: Optional[ResourceGauge] = None) -> APIRouter:
    """Build the health/pipelines router bound to the given config + registry.

    `supervisor` (ISSUE_10) adds the live worker states to /health — the first
    surface of the engine's background heartbeat (the live display #26 builds on it).
    `budget_guard` (ISSUE_47) adds the cost circuit-breaker state (suspended? until when?).
    `stall_watchdog` (ISSUE_75) adds which workers have gone silent — the worker states above
    already carried `last_run_at`, but reading a stall out of it required knowing each worker's
    threshold; this reports the verdict instead of the raw material.
    """
    router = APIRouter(prefix='/v1', tags=['health'])

    @router.get('/health', response_model=HealthResponse)
    def health() -> HealthResponse:
        workers = ([WorkerInfo(**vars(state)) for state in supervisor.states()]
                   if supervisor is not None else [])
        budget = BudgetInfo(**budget_guard.status()) if budget_guard is not None else None
        stall = StallInfo(**stall_watchdog.status()) if stall_watchdog is not None else None
        resources = (ResourceInfo(**resource_gauge.status())
                     if resource_gauge is not None else None)
        return HealthResponse(version=config_manager.get_config().version,
                              pass_timeout_seconds=config_manager.get_config().pass_timeout_seconds,
                              workers=workers, budget=budget, stall=stall,
                              resources=resources)

    @router.get('/pipelines', response_model=PipelinesResponse)
    def list_pipelines() -> PipelinesResponse:
        infos = [
            PipelineInfo(
                pipeline_id=pipeline.get_config().pipeline_id,
                outcome_type=pipeline.get_config().outcome_type,
                market=pipeline.get_config().market,
                symbols=pipeline.get_config().symbol_keys(),
                trigger_type=pipeline.get_config().trigger.type,
                cadence_seconds=_cadence_seconds(pipeline.get_config().trigger.timeframe),
            )
            for pipeline in registry.list_pipelines()
        ]
        return PipelinesResponse(pipelines=infos)

    return router
