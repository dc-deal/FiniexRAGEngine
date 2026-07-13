"""Health + pipeline-listing endpoints."""
from fastapi import APIRouter

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.api_types import (
    HealthResponse,
    PipelineInfo,
    PipelinesResponse,
    WorkerInfo,
)


def build_health_router(config_manager: AppConfigManager,
                        registry: PipelineRegistry,
                        supervisor=None) -> APIRouter:
    """Build the health/pipelines router bound to the given config + registry.

    `supervisor` (ISSUE_10) adds the live worker states to /health — the first
    surface of the engine's background heartbeat (the live display #26 builds on it).
    """
    router = APIRouter(prefix='/v1', tags=['health'])

    @router.get('/health', response_model=HealthResponse)
    def health() -> HealthResponse:
        workers = ([WorkerInfo(**vars(state)) for state in supervisor.states()]
                   if supervisor is not None else [])
        return HealthResponse(version=config_manager.get_config().version,
                              workers=workers)

    @router.get('/pipelines', response_model=PipelinesResponse)
    def list_pipelines() -> PipelinesResponse:
        infos = [
            PipelineInfo(
                pipeline_id=pipeline.get_config().pipeline_id,
                outcome_type=pipeline.get_config().outcome_type,
                market=pipeline.get_config().market,
                symbols=pipeline.get_config().symbols,
                trigger_type=pipeline.get_config().trigger.type,
            )
            for pipeline in registry.list_pipelines()
        ]
        return PipelinesResponse(pipelines=infos)

    return router
