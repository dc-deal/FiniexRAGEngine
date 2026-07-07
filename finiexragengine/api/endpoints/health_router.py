"""Health + pipeline-listing endpoints."""
from fastapi import APIRouter

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.api_types import (
    HealthResponse,
    PipelineInfo,
    PipelinesResponse,
)


def build_health_router(config_manager: AppConfigManager,
                        registry: PipelineRegistry) -> APIRouter:
    """Build the health/pipelines router bound to the given config + registry."""
    router = APIRouter(prefix='/v1', tags=['health'])

    @router.get('/health', response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(version=config_manager.get_config().version)

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
