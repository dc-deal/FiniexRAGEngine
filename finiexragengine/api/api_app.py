"""FastAPI application factory."""
import logging
import os
from typing import Optional

from fastapi import FastAPI

from finiexragengine.api.endpoints.health_router import build_health_router
from finiexragengine.api.endpoints.sentiment_router import build_sentiment_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.llm.model_catalog import verify_configured_models
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry

logger = logging.getLogger(__name__)


def create_app(attach_runners: Optional[bool] = None) -> FastAPI:
    """Build the FastAPI app with pipelines loaded and routers mounted.

    Args:
        attach_runners: None (default) attaches the real staged runners when
            DATABASE_URL is set — the production path. **False forces scaffold-mock
            mode regardless of the environment** — the contract-test path: a real
            runner behind `/run` makes paid API calls, and the free suite must never
            spend budget just because DATABASE_URL/OPENAI_API_KEY happen to be set.

    Returns:
        The configured FastAPI application.
    """
    # Boot sequence: load app config → discover + validate constellations → attach the
    # real runners → build the app → mount routers. Dependencies are wired here and
    # injected into the routers (build_*_router takes them as args) — no globals.
    config_manager = AppConfigManager()
    registry = PipelineRegistry(config_manager.get_pipelines_dir())
    registry.load()

    # Real staged flow (ISSUE_7) needs the pgvector Postgres; without DATABASE_URL the
    # pipelines keep their scaffold mock so the API still boots (contract tests, dev
    # without a DB). With it set, a failing attach is a hard boot error — fail fast,
    # never serve half-wired pipelines.
    database_url = os.environ.get('DATABASE_URL')
    if attach_runners is None:
        attach_runners = database_url is not None
    outcome_store = None
    if attach_runners:
        if not database_url:
            raise RuntimeError('attach_runners=True requires DATABASE_URL')
        assembler = PipelineAssembler(config_manager, database_url)
        assembler.attach_all(registry)
        # /latest serves from the same store every runner persists into (ISSUE_8).
        outcome_store = assembler.get_outcome_store()
        # Startup model check (ISSUE_40): free provider call, soft by design — a typo'd
        # or retired model (eval allowlist AND the corpus-binding embedding model) warns
        # loudly here instead of failing a paid run later; an unreachable provider only
        # logs (the allowlist stays the hard gate).
        verify_configured_models(config_manager.get_config())
    else:
        logger.warning('runners not attached — pipelines run in scaffold-mock mode')

    app = FastAPI(
        title='FiniexRAGEngine',
        version=config_manager.get_config().version,
    )
    app.include_router(build_health_router(config_manager, registry))
    app.include_router(build_sentiment_router(registry, outcome_store=outcome_store))
    return app
