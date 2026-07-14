"""FastAPI application factory."""
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from finiexragengine.api.endpoints.health_router import build_health_router
from finiexragengine.api.endpoints.sentiment_router import build_sentiment_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.llm.model_catalog import verify_configured_models
from finiexragengine.core.observability.logging_setup import configure_logging
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor

logger = logging.getLogger(__name__)


def create_app(attach_runners: Optional[bool] = None,
               start_workers: Optional[bool] = None) -> FastAPI:
    """Build the FastAPI app with pipelines loaded and routers mounted.

    Args:
        attach_runners: None (default) attaches the real staged runners when
            DATABASE_URL is set — the production path. **False forces scaffold-mock
            mode regardless of the environment** — the contract-test path: a real
            runner behind `/run` makes paid API calls, and the free suite must never
            spend budget just because DATABASE_URL/OPENAI_API_KEY happen to be set.
        start_workers: None (default) reads the FINIEX_WORKERS env flag (set by
            `server_cli --workers`). True runs the background heartbeat (ISSUE_10):
            ingest + eval workers on their own cadences — **continuous paid
            activity**, so it is opt-in, never a side effect of booting.

    Returns:
        The configured FastAPI application.
    """
    # Boot sequence: load app config → discover + validate constellations → attach the
    # real runners → build the app → mount routers. Dependencies are wired here and
    # injected into the routers (build_*_router takes them as args) — no globals.
    config_manager = AppConfigManager()
    # Levelled logging per app config (CLAUDE.md): uvicorn only configures its own loggers —
    # without this the workers' INFO pass lines (incl. spend, ISSUE_10) would be invisible.
    # configure_logging adds a console handler *and* a daily-rotating file so an overnight
    # worker run survives the scrollback (ISSUE_11), and quiets httpx's per-request noise.
    configure_logging(config_manager.get_config())
    registry = PipelineRegistry(config_manager.get_pipelines_dir(),
                                config_manager.get_user_pipelines_dir())
    registry.load()

    # Real staged flow (ISSUE_7) needs the pgvector Postgres; without DATABASE_URL the
    # pipelines keep their scaffold mock so the API still boots (contract tests, dev
    # without a DB). With it set, a failing attach is a hard boot error — fail fast,
    # never serve half-wired pipelines.
    database_url = os.environ.get('DATABASE_URL')
    if attach_runners is None:
        attach_runners = database_url is not None
    if start_workers is None:
        start_workers = os.environ.get('FINIEX_WORKERS') == '1'
    outcome_store = None
    supervisor = None
    if attach_runners:
        if not database_url:
            raise RuntimeError('attach_runners=True requires DATABASE_URL')
        assembler = PipelineAssembler(config_manager, database_url)
        # Worker mode (ISSUE_10): acquisition belongs to the ingest workers' clocks,
        # so the API runners are built ingest-less — /run cannot double-ingest next
        # to a running worker. Without workers, /run stays self-contained as before.
        assembler.attach_all(registry, include_ingest=not start_workers)
        # /latest serves from the same store every runner persists into (ISSUE_8).
        outcome_store = assembler.get_outcome_store()
        # Startup model check (ISSUE_40): free provider call, soft by design — a typo'd
        # or retired model (eval allowlist AND the corpus-binding embedding model) warns
        # loudly here instead of failing a paid run later; an unreachable provider only
        # logs (the allowlist stays the hard gate).
        verify_configured_models(config_manager.get_config())
        if start_workers:
            supervisor = WorkerSupervisor(assembler, registry)
    else:
        if start_workers:
            raise RuntimeError('workers need real runners — set DATABASE_URL '
                               '(scaffold-mock mode cannot ingest or evaluate)')
        logger.warning('runners not attached — pipelines run in scaffold-mock mode')

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The background heartbeat lives inside the server process: started once the
        # event loop exists, stopped on shutdown after in-flight passes finish.
        if supervisor is not None:
            await supervisor.start_all()
        yield
        if supervisor is not None:
            await supervisor.stop_all()

    app = FastAPI(
        title='FiniexRAGEngine',
        version=config_manager.get_config().version,
        lifespan=lifespan,
    )
    app.include_router(build_health_router(config_manager, registry,
                                           supervisor=supervisor))
    app.include_router(build_sentiment_router(registry, outcome_store=outcome_store))
    return app
