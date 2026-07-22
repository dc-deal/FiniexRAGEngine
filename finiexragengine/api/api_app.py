"""FastAPI application factory."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

from fastapi import FastAPI

from finiexragengine.api.endpoints.health_router import build_health_router
from finiexragengine.api.endpoints.sentiment_router import build_sentiment_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.alerts.telegram_client import TelegramClient
from finiexragengine.core.alerts.telegram_command_poller import TelegramCommandPoller
from finiexragengine.core.alerts.telegram_weekly_format import render_weekly_messages
from finiexragengine.core.alerts.weekly_scheduler import WeeklyScheduler
from finiexragengine.core.llm.model_catalog import verify_configured_models
from finiexragengine.core.observability.logging_setup import configure_logging
from finiexragengine.core.observability.reports.weekly_report import collect_weekly_report
from finiexragengine.core.outcome.outcome_exporter import auto_export_weekly
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor
from finiexragengine.core.ui.engine_stats import EngineStats
from finiexragengine.core.ui.live_display import LiveDisplay

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
    # Live-display mode (ISSUE_26): server_cli sets FINIEX_LIVE when --live wins its TTY/workers
    # guards. In live mode rich.Live owns the terminal, so the console log handler is suppressed
    # (the rotating file keeps recording); server_cli also routed uvicorn's own logs to the file.
    # Both flags: server_cli sets FINIEX_LIVE only alongside FINIEX_WORKERS, so requiring both
    # here means a stray FINIEX_LIVE never suppresses the console without a dashboard to replace it.
    live_mode = (os.environ.get('FINIEX_LIVE') == '1'
                 and os.environ.get('FINIEX_WORKERS') == '1')
    # Levelled logging per app config (CLAUDE.md): uvicorn only configures its own loggers —
    # without this the workers' INFO pass lines (incl. spend, ISSUE_10) would be invisible.
    # configure_logging adds a console handler (unless live_mode) *and* a daily-rotating file so an
    # overnight worker run survives the scrollback (ISSUE_11), and quiets httpx's per-request noise.
    configure_logging(config_manager.get_config(), live_mode=live_mode)
    registry = config_manager.build_pipeline_registry()

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
    budget_guard = None
    # Live dashboard's shared state (ISSUE_26): built only in live mode, injected into every
    # worker so each pass pushes its snapshot/events; None otherwise (zero overhead). Keys are
    # pre-registered from the same ids the supervisor builds workers from, so the dashboard's
    # per-worker dicts never resize at runtime (lock-free render).
    engine_stats: Optional[EngineStats] = None
    if live_mode:
        pipeline_ids = [pipeline.get_config().pipeline_id for pipeline in registry.list_pipelines()]
        source_set_ids = sorted({pipeline.get_config().source_set
                                 for pipeline in registry.list_pipelines()})
        engine_stats = EngineStats(source_set_ids=source_set_ids, pipeline_ids=pipeline_ids)
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
        # The cost circuit-breaker state is surfaced on /health (ISSUE_47).
        budget_guard = assembler.get_budget_guard()
        # Startup model check (ISSUE_40): free provider call, soft by design — a typo'd
        # or retired model (eval allowlist AND the corpus-binding embedding model) warns
        # loudly here instead of failing a paid run later; an unreachable provider only
        # logs (the allowlist stays the hard gate).
        verify_configured_models(config_manager.get_config())
        if start_workers:
            supervisor = WorkerSupervisor(assembler, registry, engine_stats=engine_stats)
    else:
        if start_workers:
            raise RuntimeError('workers need real runners — set DATABASE_URL '
                               '(scaffold-mock mode cannot ingest or evaluate)')
        logger.warning('runners not attached — pipelines run in scaffold-mock mode')

    # Live terminal dashboard (ISSUE_26): only when live mode won its guards in server_cli AND
    # workers run — it renders the workers' shared EngineStats plus the live BudgetGuard state on
    # an interval. The console log handler is already suppressed above, so it owns the terminal.
    live_display: Optional[LiveDisplay] = None
    if live_mode and supervisor is not None and engine_stats is not None:
        live_display = LiveDisplay(engine_stats, budget_guard=budget_guard,
                                   worker_count=len(supervisor.states()))

    # Operator alert surface (ISSUE_27): /report command loop + the weekly cron. Lives in
    # the API process like the workers (guaranteed event loop); pure store reads + a
    # Telegram send — no paid calls, so no FINIEX_WORKERS gate, but the report needs the
    # store: DATABASE_URL gates it alongside the credentials.
    telegram_client: Optional[TelegramClient] = None
    command_poller: Optional[TelegramCommandPoller] = None
    weekly_scheduler: Optional[WeeklyScheduler] = None
    telegram_cfg = config_manager.get_config().telegram
    weekly_cfg = config_manager.get_config().weekly_report
    if telegram_cfg.enabled:
        if not (telegram_cfg.bot_token and telegram_cfg.chat_id and database_url):
            logger.warning('telegram.enabled but bot_token/chat_id (user_configs) or '
                           'DATABASE_URL missing — alert surface stays off')
        else:
            telegram_client = TelegramClient(telegram_cfg)

            async def _weekly_messages() -> List[str]:
                # Build off-loop (sync psycopg reads) — the API stays responsive.
                report = await asyncio.to_thread(collect_weekly_report,
                                                 config_manager, database_url)
                return render_weekly_messages(report)

            async def _send_weekly() -> None:
                # Durable artifact first: dump the closed-day archive (default on), independent
                # of delivery — a failed Telegram send must not cost the export. Off-loop: it is
                # blocking DB reads + file writes.
                result = await asyncio.to_thread(auto_export_weekly, weekly_cfg, database_url)
                if result is not None:
                    logger.info('weekly export: %d file(s), %d line(s) → %s',
                                len(result.files), result.total_lines, weekly_cfg.export_dir)
                await telegram_client.send_messages(await _weekly_messages())

            # The command poller is a separate opt-in: it long-polls getUpdates, and
            # Telegram allows only one poller per bot — so it stays off unless this engine
            # owns a bot no other service polls (see TelegramConfig). Sending (the weekly
            # cron below) is unaffected and works on a shared bot.
            if telegram_cfg.commands_enabled:
                command_poller = TelegramCommandPoller(telegram_client, telegram_cfg,
                                                       _weekly_messages)
            if weekly_cfg.enabled:
                weekly_scheduler = WeeklyScheduler(weekly_cfg, _send_weekly)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The background heartbeat lives inside the server process: started once the
        # event loop exists, stopped on shutdown after in-flight passes finish.
        live_task: Optional[asyncio.Task] = None
        if supervisor is not None:
            await supervisor.start_all()
        # The dashboard renders on its own task once the workers exist (ISSUE_26).
        if live_display is not None:
            live_task = asyncio.create_task(live_display.run(), name='live-display')
        if command_poller is not None:
            await command_poller.start()
        if weekly_scheduler is not None:
            weekly_scheduler.start()
        yield
        if weekly_scheduler is not None:
            weekly_scheduler.stop()
        if command_poller is not None:
            await command_poller.stop()
        if telegram_client is not None:
            await telegram_client.close()
        if supervisor is not None:
            await supervisor.stop_all()
        # Stop the display last, so it shows the drained state, then releases the terminal.
        if live_display is not None:
            await live_display.stop()
            if live_task is not None:
                await live_task

    app = FastAPI(
        title='FiniexRAGEngine',
        version=config_manager.get_config().version,
        lifespan=lifespan,
    )
    app.include_router(build_health_router(config_manager, registry,
                                           supervisor=supervisor, budget_guard=budget_guard))
    app.include_router(build_sentiment_router(registry, outcome_store=outcome_store))
    return app
