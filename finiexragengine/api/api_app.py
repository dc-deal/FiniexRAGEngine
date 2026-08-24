"""FastAPI application factory."""
import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, FastAPI

from finiexragengine.api.bearer_auth import build_bearer_dependency
from finiexragengine.api.endpoints.health_router import build_health_router
from finiexragengine.api.endpoints.pipelines_router import build_pipelines_router
from finiexragengine.api.endpoints.sentiment_router import build_sentiment_router
from finiexragengine.api.rate_limiter import RateLimiter, build_rate_limit_dependency
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.alerts.telegram_client import TelegramClient
from finiexragengine.core.alerts.telegram_command_poller import TelegramCommandPoller
from finiexragengine.core.alerts.telegram_weekly_format import render_weekly_messages
from finiexragengine.core.alerts.weekly_scheduler import WeeklyScheduler
from finiexragengine.core.llm.model_catalog import verify_configured_models
from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.observability.logging_setup import configure_logging
from finiexragengine.core.observability.reports.weekly_report import collect_weekly_report
from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.core.observability.resource_sample_store import ResourceSampleStore
from finiexragengine.core.observability.stall_watchdog import StallWatchdog
from finiexragengine.core.outcome.outcome_exporter import auto_export_weekly
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor
from finiexragengine.core.ui.engine_stats import EngineStats
from finiexragengine.core.ui.live_display import LiveDisplay
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import ApiConfig

logger = logging.getLogger(__name__)


def _report_journal_identity(config_manager: AppConfigManager,
                             outcome_store: OutcomeStore) -> bool:
    """Name the journal at boot, and say so loudly when nobody has named it (ISSUE_9).

    An unnamed journal is not a broken engine — it produces and serves exactly as before. What it
    cannot do is prove *which* producer a measurement came from, and that is the whole point of the
    consumer's release certificate. Left to be discovered, it is discovered when the certificate
    comes out reading `unknown`, which is late and expensive.

    The warning carries the fingerprint and the snippet, because a warning that reports a problem
    without the value needed to fix it just moves the work.

    Returns whether the journal is named, so the live display can surface the same condition without
    resolving it a second time — `--live` suppresses the console handler, so the warning below never
    reaches an operator watching the dashboard.
    """
    journal_id = outcome_store.journal_id()
    if journal_id is None:
        logger.warning('[JOURNAL] identity unavailable — /health reports environment "unknown". '
                       'A consumer cannot record which producer it measured.')
        return False
    name = config_manager.get_config().journal_names.get(journal_id)
    if name is not None:
        logger.info('[JOURNAL] %s · %s', journal_id, name)
        return True
    # Name the ids that ARE mapped when there are any. Once the field exists, the likely mistake is
    # no longer "nobody filled it in" but "it was filled in for a different database" — a copied
    # config, a restored cluster, a second deployment. Reporting only "unnamed" would leave the
    # operator comparing two fingerprints by hand, one of which is not on screen.
    mapped = sorted(config_manager.get_config().journal_names)
    mismatch = (f' Mapped ids: {", ".join(mapped)} — none is this one.' if mapped else '')
    logger.warning(
        '[JOURNAL] %s is unnamed — /health reports environment "unknown".%s '
        'MANDATORY before a consumer connects: their release certificate records which producer it '
        'was taken against, and "unknown" makes it unfalsifiable. Add to '
        'user_configs/app_config.json (never to tracked config, it would be inherited by every '
        'fork): {"journal_names": {"%s": "production"}} · '
        'docs/development/diagnostics.md — "Which instance am I looking at?"',
        journal_id, mismatch, journal_id)
    return False


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
    # Process-wide socket deadline (ISSUE_73). The feed path already carries its own explicit
    # timeout via `_TimeoutHandler`, on every entry point including `ingest_cli` — this is the net
    # under *any other* un-timeouted socket in a process that runs for weeks. Without it Python's
    # default is `None` (block forever), which is what cost nine days on 2026-08-01. Blast radius
    # checked: httpx (OpenAI, Telegram) sets its own timeouts and libpq is C-level, so neither is
    # affected; only sockets nobody gave a deadline inherit this one.
    socket.setdefaulttimeout(config_manager.get_config().socket_default_timeout_seconds)
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
        # Which journal this engine writes into, and whether anyone has named it (ISSUE_9).
        journal_named = _report_journal_identity(config_manager, outcome_store)
        # The cost circuit-breaker state is surfaced on /health (ISSUE_47).
        budget_guard = assembler.get_budget_guard()
        # Startup model check (ISSUE_40): free provider call, soft by design — a typo'd
        # or retired model (eval allowlist AND the corpus-binding embedding model) warns
        # loudly here instead of failing a paid run later; an unreachable provider only
        # logs (the allowlist stays the hard gate).
        verify_configured_models(config_manager.get_config())
        if start_workers:
            supervisor = WorkerSupervisor(
                assembler, registry,
                pass_timeout_seconds=config_manager.get_config().pass_timeout_seconds,
                engine_stats=engine_stats)
    else:
        if start_workers:
            raise RuntimeError('workers need real runners — set DATABASE_URL '
                               '(scaffold-mock mode cannot ingest or evaluate)')
        logger.warning('runners not attached — pipelines run in scaffold-mock mode')

    # Worker liveness watchdog (ISSUE_75): reads the supervisor's own WorkerStates, so it needs no
    # new capture — only workers can stall, hence the gate. Its alert sink is wired further down,
    # once the Telegram client exists; detection and logging work regardless of delivery.
    stall_watchdog: Optional[StallWatchdog] = None
    # Process resource gauge (ISSUE_89) — rides the watchdog's tick rather than opening a second
    # loop on the same cadence. Gated on workers for the same reason the watchdog is: an API-only
    # process has nothing accumulating worth a fourteen-day series. The gauge disables itself when
    # psutil is missing, so a deploy that forgot `pip install` degrades instead of failing to boot.
    resource_gauge: Optional[ResourceGauge] = None
    if supervisor is not None:
        stall_watchdog = StallWatchdog(config_manager.get_config().stall_watchdog,
                                       supervisor.states)
        diagnostics = config_manager.get_config().diagnostics
        resource_gauge = ResourceGauge(
            store=(ResourceSampleStore(
                database_url, retention_days=diagnostics.resource_retention_days)
                if database_url else None),
            enabled=diagnostics.resource_gauge_enabled,
            rss_warn_mb=diagnostics.resource_rss_warn_mb)
        stall_watchdog.set_gauge(resource_gauge)

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
            # Give the watchdog a voice (ISSUE_75). Without Telegram it still detects and logs —
            # delivery is the optional half, so a missing credential degrades the alert, never
            # the detection.
            if stall_watchdog is not None:
                stall_watchdog.set_alert(telegram_client.send_message)
            # The same voice for a set-wide connectivity failure (ISSUE_84). The watchdog cannot
            # cover this one: during a connectivity outage every ingest pass still *completes*,
            # it just fails every poll — so without this the condition is silent until the
            # weekly report.
            if supervisor is not None:
                supervisor.set_host_alert(telegram_client.send_message)

    # Live terminal dashboard (ISSUE_26): only when live mode won its guards in server_cli AND
    # workers run — it renders the workers' shared EngineStats plus the live BudgetGuard state on
    # an interval. The console log handler is already suppressed above, so it owns the terminal.
    # Built after the watchdog so it can read its stall set: one threshold definition, not two.
    live_display: Optional[LiveDisplay] = None
    if live_mode and supervisor is not None and engine_stats is not None:
        live_display = LiveDisplay(engine_stats, budget_guard=budget_guard,
                                   stall_watchdog=stall_watchdog,
                                   resource_gauge=resource_gauge,
                                   worker_count=len(supervisor.states()),
                                   states_provider=supervisor.states,
                                   version=config_manager.get_config().version,
                                   journal_named=journal_named)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The background heartbeat lives inside the server process: started once the
        # event loop exists, stopped on shutdown after in-flight passes finish.
        live_task: Optional[asyncio.Task] = None
        watchdog_task: Optional[asyncio.Task] = None
        if supervisor is not None:
            await supervisor.start_all()
        # Watch the workers from the moment they exist (ISSUE_75) — a stall during the very first
        # passes is exactly as blinding as one on day nine.
        if stall_watchdog is not None:
            watchdog_task = asyncio.create_task(stall_watchdog.run(), name='stall-watchdog')
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
        # Stop watching before the workers drain — an orderly shutdown is not a stall.
        if stall_watchdog is not None:
            await stall_watchdog.stop()
            if watchdog_task is not None:
                await watchdog_task
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
    # ISSUE_98 — the HTTP surface splits in two, and the split IS the security design.
    #
    # The bearer dependency sits on the protected `APIRouter` itself, never on individual routes,
    # so a route added later inherits it *by construction*. The failure this issue exists to
    # prevent — an endpoint shipped unprotected because someone forgot a decorator — stops being
    # a thing anyone can forget. `tests/test_api_auth.py` asserts it on a route registered inside
    # the test, so the guarantee is checked rather than described.
    api_config = config_manager.get_config().api
    app.include_router(_build_public_router(config_manager, api_config, supervisor=supervisor,
                                            budget_guard=budget_guard,
                                            stall_watchdog=stall_watchdog,
                                            resource_gauge=resource_gauge,
                                            outcome_store=outcome_store))
    app.include_router(_build_protected_router(registry, api_config,
                                               outcome_store=outcome_store))
    return app


def _build_public_router(config_manager: AppConfigManager,
                         api_config: ApiConfig,
                         supervisor: Optional[WorkerSupervisor] = None,
                         budget_guard: Optional[BudgetGuard] = None,
                         stall_watchdog: Optional[StallWatchdog] = None,
                         resource_gauge: Optional[ResourceGauge] = None,
                         outcome_store: Optional[OutcomeStore] = None) -> APIRouter:
    """`/health` — the one documented exemption from authentication.

    An uptime probe needs it without a credential. What that publishes is not a bare `ok`:
    journal identity, worker cadences, budget and stall state. The exemption is accepted with that
    understood and written down (`docs/architecture/health_contract.md`) rather than implied.

    It carries the rate limit instead, because it is the only route an anonymous caller can reach
    at all. `health_public: false` moves it behind the token like everything else.
    """
    health = build_health_router(config_manager, supervisor=supervisor,
                                 budget_guard=budget_guard, stall_watchdog=stall_watchdog,
                                 resource_gauge=resource_gauge, outcome_store=outcome_store)
    if not api_config.health_public:
        return health
    limiter = RateLimiter(api_config.rate_limit_per_minute)
    public = APIRouter(dependencies=[Depends(build_rate_limit_dependency(limiter))])
    public.include_router(health)
    return public


def _build_protected_router(registry: PipelineRegistry,
                            api_config: ApiConfig,
                            outcome_store: Optional[OutcomeStore] = None) -> APIRouter:
    """Everything a token is required for — and everything added here later, automatically."""
    # Environment wins, the config overlay fills in — see `TokenRegistry.load`. The source is
    # announced below rather than inferred: a value in `user_configs` silently shadowed by a stale
    # environment variable is precisely the kind of no-op that costs an afternoon to find.
    tokens = TokenRegistry.load(api_config.tokens)
    if api_config.require_auth and tokens.is_empty():
        # A hard boot failure, not a warning. Starting unprotected because an environment variable
        # was missing is precisely the accident ISSUE_98 was written about, and a warning in a log
        # nobody reads at 3 a.m. is not a control.
        raise ConfigurationError(
            'api.require_auth is on but no consumer tokens are configured — refusing to serve an '
            'unauthenticated API. Set them in user_configs/app_config.json under api.tokens, or '
            'out of band as FINIEX_API_TOKENS="<consumer>:<token>" (never in a pasteable startup '
            'script) · see docs/architecture/connect_contract.md')

    guards = []
    if api_config.require_auth:
        auth_limiter = RateLimiter(api_config.auth_failures_per_minute)
        guards.append(Depends(build_bearer_dependency(tokens, auth_limiter)))
        logger.info('[AUTH] %d consumer token(s): %s · /health %s · POST /run %s',
                    len(tokens.names()), ', '.join(tokens.names()) or '—',
                    'public' if api_config.health_public else 'protected',
                    'enabled' if api_config.run_endpoint_enabled else 'DISABLED')
    else:
        # Scaffold-mock and contract tests. Loud, because an unauthenticated engine reachable from
        # anywhere is the state this issue closed.
        logger.warning('[AUTH] authentication is OFF (api.require_auth=false) — every route is '
                       'open to anyone who can reach this process')

    protected = APIRouter(dependencies=guards)
    protected.include_router(build_pipelines_router(registry))
    protected.include_router(build_sentiment_router(
        registry, outcome_store=outcome_store, run_enabled=api_config.run_endpoint_enabled))
    return protected
