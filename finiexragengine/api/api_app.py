"""FastAPI application factory."""
import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, FastAPI

from finiexragengine.api.bearer_auth import build_bearer_dependency
from finiexragengine.api.grant_auth import build_grant_dependency
from finiexragengine.api.endpoints.build_router import build_build_router
from finiexragengine.api.endpoints.envelopes_router import build_envelopes_router
from finiexragengine.api.endpoints.health_router import build_health_router
from finiexragengine.api.endpoints.report_router import build_report_router
from finiexragengine.api.endpoints.pipelines_router import build_pipelines_router
from finiexragengine.api.endpoints.sentiment_router import build_sentiment_router
from finiexragengine.api.endpoints.stream_router import build_stream_router
from finiexragengine.api.rate_limiter import RateLimiter, build_rate_limit_dependency
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.alerts.telegram_client import TelegramClient
from finiexragengine.core.alerts.telegram_command_poller import TelegramCommandPoller
from finiexragengine.core.alerts.telegram_weekly_format import render_weekly_messages
from finiexragengine.core.alerts.weekly_scheduler import WeeklyScheduler
from finiexragengine.core.llm.model_catalog import verify_configured_models
from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.observability.build_info import sample_build_info
from finiexragengine.core.observability.logging_setup import configure_logging
from finiexragengine.core.observability.reports.weekly_report import collect_weekly_report
from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.core.observability.resource_sample_store import ResourceSampleStore
from finiexragengine.core.observability.stall_watchdog import StallWatchdog
from finiexragengine.core.outcome.outcome_exporter import auto_export_weekly
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.core.pipeline.detection_preflight import log_detection_preflight
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.pipeline.worker_supervisor import WorkerSupervisor
from finiexragengine.core.ui.engine_stats import EngineStats
from finiexragengine.core.ui.live_display import LiveDisplay
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import ApiConfig, StreamConfig

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
    # The stream's two units (ISSUE_9): the journal tailer and the replay policy. None without a
    # database — a stream over no journal has nothing to tail and would answer every connect with a
    # cold start that is not true.
    stream_dispatcher: Optional[StreamDispatcher] = None
    stream_replay: Optional[StreamReplay] = None
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
        # Detection-threshold preflight (ISSUE_106) — the same idiom one domain over: the three
        # `DetectionConfig` thresholds only mean something relative to the feeds that actually run,
        # and nothing checked them until now. Warns, never refuses: an over-ambitious threshold is
        # a degraded feature, and blocking boot on it would take the engine down over a quarantined
        # feed. Read through the registry factory, so the `user_configs/` overlay is honoured — a
        # per-machine `enabled: false` is precisely what moves these counts.
        log_detection_preflight(
            config_manager.build_source_set_registry().list_sets())
        # The live stream's journal tailer (ISSUE_9). Built whenever there is a store and the
        # transport is enabled — deliberately NOT gated on `start_workers`: the stream is a read
        # surface over the journal, so it serves a journal another process writes. That is what lets
        # a dev instance serve the live contract to a consumer without making a single paid call.
        stream_config = config_manager.get_config().stream
        if stream_config.enabled:
            stream_dispatcher = StreamDispatcher(
                outcome_store, database_url,
                notify_channel=stream_config.notify_channel,
                fallback_poll_seconds=stream_config.fallback_poll_seconds,
                subscriber_queue_size=stream_config.subscriber_queue_size)
            stream_replay = StreamReplay(outcome_store, stream_config.replay_window_hours,
                                         stream_config.max_replay_frames)
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
        stream_task: Optional[asyncio.Task] = None
        # The dispatcher first: a subscriber attaching in the first milliseconds of the process must
        # find a tail already running, and it costs nothing when nobody is watching (a stream with no
        # subscriptions is not read at all).
        if stream_dispatcher is not None:
            stream_task = asyncio.create_task(stream_dispatcher.run(), name='stream-dispatcher')
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
        # After the workers: nothing new commits from here, so the tail can end having delivered
        # everything the last pass produced.
        if stream_dispatcher is not None:
            await stream_dispatcher.stop()
            if stream_task is not None:
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
        # Stop the display last, so it shows the drained state, then releases the terminal.
        if live_display is not None:
            await live_display.stop()
            if live_task is not None:
                await live_task

    # The interactive schema surfaces (ISSUE_98). FastAPI mounts them on the app itself, so the
    # protected router's dependency never sees them — passing None is the only way to keep them
    # off. They map the entire API for anyone who asks, so they are opt-in like `/run`.
    api_config = config_manager.get_config().api
    docs = api_config.docs_enabled
    app = FastAPI(
        title='FiniexRAGEngine',
        version=config_manager.get_config().version,
        lifespan=lifespan,
        docs_url='/docs' if docs else None,
        redoc_url='/redoc' if docs else None,
        openapi_url='/openapi.json' if docs else None,
    )
    # ISSUE_98 — the HTTP surface splits in two, and the split IS the security design.
    #
    # The bearer dependency sits on the protected `APIRouter` itself, never on individual routes,
    # so a route added later inherits it *by construction*. The failure this issue exists to
    # prevent — an endpoint shipped unprotected because someone forgot a decorator — stops being
    # a thing anyone can forget. `tests/api/test_api_auth.py` asserts it on a route registered inside
    # the test, so the guarantee is checked rather than described.
    #
    # Which side an exempt route lands on is decided here, once, and it is mounted on exactly one
    # of the two. `health_public: false` used to leave /health on the app regardless — public, and
    # *unthrottled*, because the rate limiter lives on the public wrapper. The flag documented as
    # "moves it behind the token" removed the only protection an anonymous caller met.
    health = build_health_router(config_manager, supervisor=supervisor,
                                 budget_guard=budget_guard, stall_watchdog=stall_watchdog,
                                 resource_gauge=resource_gauge, outcome_store=outcome_store)
    # Sampled once, here, so it describes the code THIS process imported rather than whatever the
    # working tree holds at request time (see `build_info.sample_build_info`).
    build = build_build_router(sample_build_info(config_manager.get_config().version))
    exempt = ((health, api_config.health_public), (build, api_config.build_info_public))
    app.include_router(_build_public_router(
        api_config, [router for router, is_public in exempt if is_public]))
    # Diagnostic reports (ISSUE_104) — protected like everything else, and only where there is a
    # store to read: without a database the catalog has nothing to answer from, and a route that
    # can only 503 is worse than one that is honestly absent.
    # One registry, two users: the bearer dependency verifies against it, and the report surface
    # asks it what a verified consumer may read (ISSUE_104). Built here rather than inside the
    # protected router so both see the identical object — a second load could disagree with the
    # first about who exists.
    tokens = TokenRegistry.load(api_config.tokens)
    protected_extra = [router for router, is_public in exempt if not is_public]
    if database_url:
        protected_extra.append(build_report_router(
            database_url, config_manager, tokens,
            max_window_days=api_config.reports_max_window_days))
    # The stream rides the protected router like everything else (ISSUE_98), and carries its own
    # `Security(..., scopes=['pipelines'])` so the grant is checked against `{pipeline_id}`.
    if stream_dispatcher is not None and stream_replay is not None:
        protected_extra.append(build_stream_router(
            stream_dispatcher, stream_replay, registry,
            config_manager.get_config().stream, build_grant_dependency(tokens)))
        # The same replay policy over plain HTTP (ISSUE_9 §2): the collector's catch-up path, and the
        # reason `/latest` is not it — everything superseded between two polls is otherwise never
        # fetched. Shares the unit, so the two surfaces cannot disagree about a cursor.
        protected_extra.append(build_envelopes_router(
            stream_replay, registry, build_grant_dependency(tokens)))
    app.include_router(_build_protected_router(
        registry, api_config, tokens, outcome_store=outcome_store,
        extra_routers=protected_extra,
        # The transport's engine-wide numbers, taken from the configuration THIS process runs on
        # (ISSUE_9). Passed explicitly rather than defaulted, so the listing cannot serve a value
        # the engine is not using.
        stream=config_manager.get_config().stream))
    return app


def _build_public_router(api_config: ApiConfig, routers: List[APIRouter]) -> APIRouter:
    """The documented exemptions from authentication, and nothing else.

    `/health` because an uptime probe needs it without a credential — and what that publishes is not
    a bare `ok`: journal identity, worker cadences, budget and stall state. `/v1/build` because a
    commit hash of a public repository discloses nothing that is not already readable on GitHub.
    Both exemptions are written down (`docs/architecture/health_contract.md`) rather than implied,
    and both are switchable: turned off, the route moves to the protected router, which is the
    behaviour the switches always claimed.

    They carry the rate limit, because they are the only routes an anonymous caller can reach at
    all. An empty list still yields a router — mounting nothing is the correct result when every
    exemption is switched off.
    """
    limiter = RateLimiter(api_config.rate_limit_per_minute)
    public = APIRouter(dependencies=[Depends(build_rate_limit_dependency(limiter))])
    for router in routers:
        public.include_router(router)
    return public


def _build_protected_router(registry: PipelineRegistry,
                            api_config: ApiConfig,
                            tokens: TokenRegistry,
                            outcome_store: Optional[OutcomeStore] = None,
                            extra_routers: Optional[List[APIRouter]] = None,
                            stream: Optional[StreamConfig] = None) -> APIRouter:
    """Everything a token is required for — and everything added here later, automatically.

    `extra_routers` carries routers assembled by the caller: the exemptions that were switched
    *off* (a disabled exemption is simply a protected route) and the report surface, which exists
    only when a database is configured.
    """
    # Environment wins, the config overlay fills in — see `TokenRegistry.load`. The source is
    # announced below rather than inferred: a value in `user_configs` silently shadowed by a stale
    # environment variable is precisely the kind of no-op that costs an afternoon to find.
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
        # Each consumer with what it may read and who holds it. A scope that is only in a config
        # file is a scope nobody checks; announced at boot it is one line in the log an operator
        # already reads (the same reasoning as SettingResolver's [SETTING] lines).
        for name in tokens.names():
            note = tokens.note_of(name)
            logger.info('[AUTH] token %s · grants: %s%s', name, tokens.grants_of(name),
                        f' · {note}' if note else '')
        for name in tokens.inactive_names():
            # A switched-off token is not an absent one, and the difference is exactly what an
            # operator needs when a consumer reports that nothing works.
            logger.warning('[AUTH] token %s · INACTIVE (configured but switched off)', name)
        logger.info('[AUTH] %d consumer token(s) · /health %s · POST /run %s',
                    len(tokens.names()),
                    'public' if api_config.health_public else 'protected',
                    'enabled' if api_config.run_endpoint_enabled else 'DISABLED')
    else:
        # Scaffold-mock and contract tests. Loud, because an unauthenticated engine reachable from
        # anywhere is the state this issue closed.
        logger.warning('[AUTH] authentication is OFF (api.require_auth=false) — every route is '
                       'open to anyone who can reach this process')

    protected = APIRouter(dependencies=guards)
    for router in extra_routers or ():
        protected.include_router(router)
    # Each domain router declares its own surface with `Security(..., scopes=[...])`; the bearer
    # dependency above runs first (outer router before inner), so `request.state.consumer` is set
    # by the time a grant is checked.
    grant = build_grant_dependency(tokens)
    # `stream` is required by the listing router on purpose; this helper defaults it only for
    # callers that do not exercise the field (the auth and scope suites), never for `create_app`.
    protected.include_router(build_pipelines_router(
        registry, stream if stream is not None else StreamConfig(), tokens, grant))
    protected.include_router(build_sentiment_router(
        registry, grant, outcome_store=outcome_store,
        run_enabled=api_config.run_endpoint_enabled))
    return protected
