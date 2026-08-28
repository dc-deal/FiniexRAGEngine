"""The health endpoint — public by exemption (ISSUE_98); `/pipelines` moved out."""
from typing import Optional

from fastapi import APIRouter

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.core.observability.stall_watchdog import StallWatchdog
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor
from finiexragengine.types.api_types import (
    BudgetInfo,
    DispatcherInfo,
    HealthResponse,
    ResourceInfo,
    StallInfo,
    WorkerInfo,
)


def build_health_router(config_manager: AppConfigManager,
                        supervisor: Optional[WorkerSupervisor] = None,
                        budget_guard: Optional[BudgetGuard] = None,
                        stall_watchdog: Optional[StallWatchdog] = None,
                        resource_gauge: Optional[ResourceGauge] = None,
                        outcome_store: Optional[OutcomeStore] = None,
                        stream_dispatcher: Optional[StreamDispatcher] = None) -> APIRouter:
    """Build the health router — the one route deliberately reachable without a token.

    `supervisor` (ISSUE_10) adds the live worker states to /health — the first
    surface of the engine's background heartbeat (the live display #26 builds on it).
    `budget_guard` (ISSUE_47) adds the cost circuit-breaker state (suspended? until when?).
    `stream_dispatcher` (ISSUE_9 follow-up) adds the push path — requested by the consumer after a
    tail that could not open its listener served connect and replay correctly while pushing nothing
    for 22 hours. It is the same rule the workers already got: the engine says when it is not working.
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
        stream = (DispatcherInfo(**stream_dispatcher.status())
                  if stream_dispatcher is not None else None)
        journal_id = outcome_store.journal_id() if outcome_store is not None else None
        # Resolved, never declared: an unmapped or unidentifiable journal is honestly `unknown`.
        environment = config_manager.get_config().journal_names.get(journal_id or '', 'unknown')
        # 'ok' is a claim, not a default. A worker whose task ended is the strongest reason to
        # withdraw it — everything that worker feeds is frozen until a restart — and a stall is the
        # weaker one. Reported together so an external check sees a single field change, which is
        # the only thing a monitor polls: this endpoint answered 'ok' for the whole 37 hours the
        # crypto ingest worker lay dead on 2026-08-20.
        unhealthy = ([worker.name for worker in workers if worker.stopped_at is not None]
                     + (list(stall.stalled) if stall is not None else []))
        return HealthResponse(status='degraded' if unhealthy else 'ok',
                              version=config_manager.get_config().version,
                              pass_timeout_seconds=config_manager.get_config().pass_timeout_seconds,
                              journal_id=journal_id,
                              environment=environment,
                              workers=workers, budget=budget, stall=stall,
                              resources=resources, stream=stream)

    return router
