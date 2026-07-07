"""FastAPI application factory."""
from fastapi import FastAPI

from finiexragengine.api.endpoints.health_router import build_health_router
from finiexragengine.api.endpoints.sentiment_router import build_sentiment_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry


def create_app() -> FastAPI:
    """Build the FastAPI app with pipelines loaded and routers mounted.

    Returns:
        The configured FastAPI application.
    """
    config_manager = AppConfigManager()
    registry = PipelineRegistry(config_manager.get_pipelines_dir())
    registry.load()

    app = FastAPI(
        title='FiniexRAGEngine',
        version=config_manager.get_config().version,
    )
    app.include_router(build_health_router(config_manager, registry))
    app.include_router(build_sentiment_router(registry))
    return app
