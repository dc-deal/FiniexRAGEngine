"""The health endpoint — public by exemption (ISSUE_98); `/pipelines` moved out."""
from typing import Dict, List, Optional

from fastapi import APIRouter

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.observability.health_verdict import engine_problems
from finiexragengine.core.observability.outside_heartbeat import OutsideHeartbeat
from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.core.observability.stall_watchdog import StallWatchdog
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor
from finiexragengine.core.schema.run_lock import RunLock
from finiexragengine.types.api_types import (
    BudgetInfo,
    DispatcherInfo,
    HealthResponse,
    HeartbeatInfo,
    ResourceInfo,
    RunLockInfo,
    StallInfo,
    WorkerInfo,
)


def build_health_router(config_manager: AppConfigManager,
                        supervisor: Optional[WorkerSupervisor] = None,
                        budget_guard: Optional[BudgetGuard] = None,
                        stall_watchdog: Optional[StallWatchdog] = None,
                        resource_gauge: Optional[ResourceGauge] = None,
                        outcome_store: Optional[OutcomeStore] = None,
                        run_lock: Optional[RunLock] = None,
                        stream_dispatcher: Optional[StreamDispatcher] = None,
                        public_worker_names: Optional[Dict[str, str]] = None,
                        heartbeat: Optional[OutsideHeartbeat] = None) -> APIRouter:
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
    `public_worker_names` maps a private worker's real name to its public alias (`eval:private-1`):
    this route is reachable without a token, and a private pipeline's name is business logic. Its
    `last_detail` is blanked for the same reason — it can name feeds — while the state stays exact,
    so liveness is still observable from outside.
    `heartbeat` adds what the dead man's switch sender did, which is what tells an unhealthy
    engine from a broken network path once the external service has raised its alarm.
    """
    aliases = public_worker_names or {}
    router = APIRouter(prefix='/v1', tags=['health'])

    @router.get('/health', response_model=HealthResponse)
    def health() -> HealthResponse:
        workers = ([_public(WorkerInfo(**vars(state)), aliases) for state in supervisor.states()]
                   if supervisor is not None else [])
        budget = BudgetInfo(**budget_guard.status()) if budget_guard is not None else None
        stall = StallInfo(**stall_watchdog.status()) if stall_watchdog is not None else None
        if stall is not None and aliases:
            stall = stall.model_copy(update={
                'stalled': [aliases.get(name, name) for name in stall.stalled]})
        resources = (ResourceInfo(**resource_gauge.status())
                     if resource_gauge is not None else None)
        stream = (DispatcherInfo(**stream_dispatcher.status())
                  if stream_dispatcher is not None else None)
        journal_id = outcome_store.journal_id() if outcome_store is not None else None
        # One level finer than the journal: which deployment inside it is producing (ISSUE_9
        # follow-up). Read from the store so this route and the envelopes report the same string.
        instance_id = outcome_store.instance_identity() if outcome_store is not None else None
        # The worker role's claim, ASKED OF THE DATABASE rather than remembered (ISSUE_126): a lock
        # whose session died quietly would otherwise report itself installed while protecting
        # nothing, and the client-side `connection.closed` cannot see a server-side kill. The answer
        # carries `checked_at`, because the check is rate-limited — this route is public, so a read
        # of it must not become a database query. Absent when this process runs no workers.
        lock = RunLockInfo(**run_lock.status()) if run_lock is not None else None
        # Resolved, never declared: an unmapped or unidentifiable journal is honestly `unknown`.
        environment = config_manager.get_config().journal_names.get(journal_id or '', 'unknown')
        # One computation with the outside check's sender, so the two cannot disagree.
        unhealthy = engine_problems(supervisor.states() if supervisor is not None else [],
                                    stall.stalled if stall is not None else [],
                                    lock.held if lock is not None else None)
        return HealthResponse(status='degraded' if unhealthy else 'ok',
                              version=config_manager.get_config().version,
                              pass_timeout_seconds=config_manager.get_config().pass_timeout_seconds,
                              journal_id=journal_id,
                              instance_id=instance_id,
                              run_lock=lock,
                              environment=environment,
                              workers=workers, budget=budget, stall=stall,
                              resources=resources, stream=stream,
                              heartbeat=(HeartbeatInfo(**heartbeat.status())
                                         if heartbeat is not None else None))

    return router


def _public(worker: WorkerInfo, aliases: Dict[str, str]) -> WorkerInfo:
    """A worker as the public route shows it — renamed and without detail when it is private."""
    alias = aliases.get(worker.name)
    if alias is None:
        return worker
    return worker.model_copy(update={'name': alias, 'last_detail': ''})


def private_worker_aliases(private_pipeline_ids: List[str],
                           private_source_set_ids: List[str]) -> Dict[str, str]:
    """`eval:<id>` / `ingest:<id>` of every private pipeline and set → a neutral numbered alias.

    Numbered per kind in sorted-id order, so the mapping is stable for a given configuration. It is
    not an identity across configurations — adding a private pipeline may renumber — which is fine
    for what the public route is for: is everything alive.
    """
    aliases: Dict[str, str] = {}
    for kind, ids in (('eval', private_pipeline_ids), ('ingest', private_source_set_ids)):
        for number, item in enumerate(sorted(ids), start=1):
            aliases[f'{kind}:{item}'] = f'{kind}:private-{number}'
    return aliases
