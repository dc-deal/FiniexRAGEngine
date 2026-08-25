"""Pydantic config schema for the application — backs AppConfigManager.

Defaults mirror configs/app_config.json exactly (operator-visible, tunable).
"""
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, field_validator

from finiexragengine.types.config_types.report_config_types import ReportsConfig


# The surfaces a grant can name. A closed vocabulary on purpose: this is the *producing* seam —
# an operator writing the config — so a typo like `report:source_health` must fail at boot rather
# than turn into a silent denial nobody can see (CLAUDE.md, closed vocabularies).
GRANT_SURFACES: Tuple[str, ...] = ('reports', 'pipelines')


class ConsumerToken(BaseModel):
    """One consumer's credential: who holds it, what it may reach, and whether it is in force.

    `grants` is **required**, and that is the point. A token without declared rights would have to
    default to something, and every safe-by-omission default is a default someone eventually relies
    on without noticing. Declaring it makes granting an act rather than an oversight: a surface
    added later is reachable by a consumer only once someone writes its name here.

    A grant is `<surface>:<name>` — `reports:source_health`, `pipelines:crypto_sentiment` — with
    `<surface>:*` for a whole surface and a bare `*` for everything. **Domain names, not routes.**
    `source_health` is a stable concept; `/v1/reports/source_health` is merely its current address,
    and binding a grant to an address means a later `/v2` or a rename silently stops matching — a
    403 for a consumer who did nothing wrong. Names also compare exactly: no wildcard matching
    against paths, which is where authorization defects live.

    `active` is a kill switch, not documentation. A consumer can be switched off without deleting
    their token — during an incident, or to keep a superseded token in place through a rotation.
    An inactive entry never enters the registry, so an example one cannot authenticate.

    `note` records who holds this token. It costs one line and answers the question that otherwise
    arrives during a rotation, months later: *who is `ide2`, and may I revoke it?*
    """
    token: str
    grants: List[str]
    active: bool = True
    note: str = ''

    @field_validator('grants')
    @classmethod
    def _grants_name_a_known_surface(cls, value: List[str]) -> List[str]:
        for grant in value:
            if grant == '*':
                continue
            surface, separator, name = grant.partition(':')
            if not separator or not name or surface not in GRANT_SURFACES:
                raise ValueError(
                    f'grant {grant!r} is not "<surface>:<name>" over a known surface. '
                    f'Surfaces: {", ".join(GRANT_SURFACES)}. Use e.g. "reports:source_health", '
                    f'"reports:*", "pipelines:crypto_sentiment", or "*" for everything')
        return value


class ApiConfig(BaseModel):
    """What the HTTP surface is allowed to do (ISSUE_98).

    `host`/`port` deliberately do **not** live here. They were declared and never read — the bind
    address comes solely from `server_cli --host`, so an override here would have been a silent
    no-op. Everything below *is* read, in `create_app`.
    """
    # Master switch for bearer authentication. Off only for the contract tests, which build the
    # app in scaffold-mock mode; a deployment that turns it off has to do so deliberately.
    require_auth: bool = True
    # The one documented exemption: an uptime probe needs /health without a credential. Note what
    # that publishes — journal identity, worker cadences, budget and stall state — accepted with
    # that understood (docs/architecture/health_contract.md).
    health_public: bool = True
    # `POST /{pipeline_id}/run` converts an HTTP request into OpenAI spend. Off means the route is
    # never registered — not registered and refusing, which would still exist and still be one
    # config edit from live. The engine's own workers produce the series; an external forced pass
    # is a development affordance.
    run_endpoint_enabled: bool = False
    # FastAPI's own `/docs`, `/redoc` and `/openapi.json`. They are mounted on the *app*, not on a
    # router, so no router-level dependency can reach them — an easy thing to assume is covered and
    # is not. They publish the full surface map: every route, every model, and any endpoint added
    # later. Off by default for the same reason `/run` is: a development affordance has to be
    # switched on deliberately, never inherited.
    docs_enabled: bool = False
    # Consumer bearer tokens, `name -> token`. **Empty in the tracked config, always** — the real
    # values belong in the gitignored `user_configs/app_config.json` overlay. The environment
    # variable FINIEX_API_TOKENS still wins when set, so a container or CI keeps working unchanged;
    # whichever source supplied them is reported at boot, so a config value shadowed by a stale
    # environment variable can never be a silent no-op.
    tokens: Dict[str, ConsumerToken] = Field(default_factory=dict)

    @field_validator('tokens', mode='before')
    @classmethod
    def _reject_the_bare_token_form(cls, value: Any) -> Any:
        """A plain `"name": "token"` used to be the whole entry. It is no longer enough.

        Rejected with a message rather than absorbed: silently reading it as "everything" would
        reinstate exactly the by-omission access this shape was introduced to end, and it would do
        so for the entries most likely to be old — the ones nobody has looked at in a while.
        """
        if not isinstance(value, dict):
            return value
        bare = sorted(name for name, entry in value.items() if isinstance(entry, str))
        if bare:
            raise ValueError(
                f'api.tokens entries must declare what they may reach: {", ".join(bare)} '
                f'still use the bare "name": "<token>" form. Write '
                f'{{"token": "<token>", "grants": ["*"], "note": "<who holds it>"}} instead — '
                f'["*"] for everything, or a list such as '
                f'["reports:source_health", "pipelines:crypto_sentiment"]')
        return value
    # `GET /v1/build` — version, commit, dirty flag, process start time. Public by default like
    # `/health`, and for a reason that is specific rather than general: this repository is public,
    # so a commit hash discloses nothing that is not already readable on GitHub. Behind a private
    # repository the same field fingerprints the exact version, and therefore its known defects —
    # hence a switch, so closing it later is a config edit and not a code change.
    build_info_public: bool = True
    # `GET /v1/reports/{name}` (ISSUE_104): the hard ceiling on a report's window, in days. A
    # report over an unbounded window is a full scan of the journal, and the caller is a diagnostic
    # tool that will ask for `all` on a table meant to grow for years. A request above this is
    # clamped rather than refused, and the response says which window it actually used.
    reports_max_window_days: int = 90
    # Rate limits, per client per minute. `/health` is the only route without a token, so it is the
    # only one an anonymous caller can flood; the second line bounds credential guessing.
    rate_limit_per_minute: int = 60
    auth_failures_per_minute: int = 10


class LlmConfig(BaseModel):
    """Call mechanics + model governance — deliberately WITHOUT a global model.

    The eval model is series-defining (like the prompt): each pipeline declares its own
    (`pipeline.llm.model`), so a global edit can never silently shift every signal
    series at once. This block only governs *how* calls are made and *which* models are
    admissible at all.
    """
    provider: str = 'openai'
    temperature: float = 0.1
    timeout_seconds: int = 30
    # Governance allowlist: a pipeline requesting a model outside this set fails at
    # assembly — fail fast, before any spend. Override the list in the gitignored
    # user_configs to admit e.g. a fine-tuned `ft:...` model without touching tracked config.
    allowed_models: List[str] = Field(
        default_factory=lambda: ['gpt-4o-mini', 'gpt-4o'])
    # Optional OpenAI-compatible endpoint (vLLM, Ollama, ...) for self-hosted models —
    # private infrastructure, so it belongs in the user_configs override.
    base_url: Optional[str] = None


class EmbeddingConfig(BaseModel):
    provider: str = 'openai'
    model: str = 'text-embedding-3-small'
    dimensions: int = 1536
    # The model's hard input limit (ISSUE_79). Model-bound like `dimensions` and not discoverable
    # from the API (`/v1/models` returns only id/created/object/owned_by), so it is declared here
    # and travels with the model: change the model and both values change together — #16's corpus
    # guard already refuses a boot when the model shifts underneath the corpus.
    max_input_tokens: int = 8192
    # Tokenizer override (ISSUE_79). None = resolve from the model via tiktoken's own table.
    # The escape hatch for a model shipped before tiktoken knows it — otherwise that is a hard
    # block on ingest with no config-level remedy.
    encoding: Optional[str] = None
    # Per-request deadline for the embeddings call (ISSUE_79). Without it the OpenAI SDK default
    # applies (600s read x 2 retries ~= 30 min) — this was the last un-timeouted network call in
    # the ingest path. Deliberately well below the worker's `pass_timeout_seconds` so the *call*
    # fails with a log line before the *pass* is abandoned without one.
    timeout_seconds: int = 60


class VectorStoreConfig(BaseModel):
    # No `table` key: the corpus table name is owned by the migrations (ISSUE_14), not by
    # config — a config value here could only ever disagree with the schema that exists.
    backend: str = 'pgvector'
    retrieval_top_k: int = 12
    recency_window_minutes: int = 1440


class ModelPrice(BaseModel):
    """USD price per 1K tokens for one model (embeddings have output_per_1k = 0)."""
    input_per_1k: float = 0.0
    output_per_1k: float = 0.0


# Published OpenAI rates per 1K tokens — there is no pricing API, so this is a
# hand-maintained table (update it when OpenAI changes prices). Mirrors
# configs/app_config.json `pricing.models`.
_DEFAULT_MODEL_PRICES = {
    'text-embedding-3-small': ModelPrice(input_per_1k=0.00002),
    'text-embedding-3-large': ModelPrice(input_per_1k=0.00013),
    'gpt-4o-mini': ModelPrice(input_per_1k=0.00015, output_per_1k=0.0006),
    'gpt-4o': ModelPrice(input_per_1k=0.0025, output_per_1k=0.01),
}


class PricingConfig(BaseModel):
    """Per-model token prices — the reproducible basis for deriving USD from usage."""
    currency: str = 'USD'
    models: Dict[str, ModelPrice] = Field(
        default_factory=lambda: dict(_DEFAULT_MODEL_PRICES))


class CircuitBreakerConfig(BaseModel):
    """Cost circuit-breaker (ISSUE_47) — react to the provider's own spend limit.

    The hard stop is the provider itself: OpenAI returns HTTP 429 `insufficient_quota` at the
    account ceiling. This block only governs how we *react* — on that signal, suspend paid work,
    back off, and re-probe once per cool-off (auto-resume). `soft_daily_usd` is an optional
    warn-only early line *under* that ceiling; it never suspends (the provider stays the hard stop).
    """
    enabled: bool = True                   # master switch for the reaction
    reprobe_interval_seconds: int = 120    # cool-off before one re-probe after a quota suspend
    soft_daily_usd: float = 0.0            # warn-only day line (0 = off); does NOT suspend


class CostConfig(BaseModel):
    """Cost tracking knobs. Balance is not exposed by the API, so we derive it."""
    account_credit_usd: float = 0.0   # what you topped up; remaining ≈ credit − tracked spend
    budget_usd: float = 0.0           # optional soft cap for a spend warning (0 = off)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)   # ISSUE_47


class LoggingConfig(BaseModel):
    """File logging + rotation (ISSUE_11). The console handler stays on regardless — this
    only adds a flat, rotating file so an overnight worker run survives the scrollback and
    stays grep-able the morning after. The *level* is `AppConfig.log_level` (shared with the
    console); this block is purely the file + noise policy.
    """
    file: Optional[str] = 'logs/finiex.log'    # rotating log path; set null for console-only
    rotation: Literal['daily', 'size'] = 'daily'
    backup_count: int = 14                     # rotated files kept (daily: days; size: files)
    max_bytes: int = 10_000_000                # size-rotation only (ignored when rotation='daily')
    # Third-party loggers pinned to WARNING so the file is signal, not per-request noise
    # (httpx logs every OpenAI call at INFO — thousands a night otherwise).
    quiet_loggers: List[str] = Field(default_factory=lambda: ['httpx', 'httpcore'])
    # Startup override report: log WHAT each user_configs/ file changes (old → new per
    # leaf, typo'd keys flagged) — once per process. False = only the compact markers.
    warn_on_override: bool = True


class SourceHealthConfig(BaseModel):
    """Source-health flagging policy (ISSUE_11, ISSUE_84) — app-wide, not per source-set.

    A feed that keeps failing (rate-limit, malformed body, TLS drop) is flagged and
    quarantined: polling pauses for a cool-off, then exactly one probe decides whether it
    recovered. The last few warnings/errors are kept per source so the Sources report /
    weekly is debugging-ready without digging through logs.

    ISSUE_84 replaced the flat 24h with the circuit-breaker shape `BudgetGuard` already owns:
    a graduated ladder, a half-open probe, and a guard against punishing feeds for what is
    plainly a local connectivity failure.
    """
    flag_after_consecutive_failures: int = 5   # consecutive fails -> flag + quarantine
    # The escalation ladder (ISSUE_84): first episode 1h, a repeat within `ladder_reset_hours`
    # 6h, the third and beyond 24h. A flat 24h treated a feed at 99.97% availability exactly
    # like one that has never answered — 3m42s of trouble on ecb_press cost a day of ingest.
    # A bare integer stays valid and means a single-rung ladder (see the validator below).
    quarantine_hours: List[int] = Field(default_factory=lambda: [1, 6, 24])
    recent_events_kept: int = 10                # capped warn/error ring per source (overview)
    # How long a feed's episodes stay "recent" for the ladder. A full window without a new
    # episode drops it back to the first rung — the memory is derived from the episode history
    # itself (a SQL count), never from a stored counter that could drift from it.
    ladder_reset_hours: int = 168               # 7 days
    # Splits the overloaded UNREACHABLE bucket: DNS/refused come back in milliseconds, a feed
    # that went quiet burns the deadline. Three orders of magnitude apart, so the cut is safe
    # anywhere in between — a failure at/above this share of the fetch deadline reads as
    # transient (short rung), below it as a durable refusal (long rung).
    deadline_ratio: float = 0.7
    # Correlated-failure guard (ISSUE_84): when this share of a pass's pollable sources fails
    # at once, the common cause is local (DNS, network, the container) and quarantining every
    # feed converts a short shared outage into a long self-inflicted one — 2026-07-29 turned
    # ~5h into ~25h that way. Its second job is protecting the ladder: without it one host
    # event escalates every healthy feed a rung.
    correlated_failure_ratio: float = 0.85
    correlated_min_pollable: int = 3            # below this a thin pass can't look "correlated"
    correlated_backoff_minutes: int = 5         # set-level pause instead of N per-feed quarantines

    @field_validator('quarantine_hours', mode='before')
    @classmethod
    def _accept_single_rung(cls, value: object) -> object:
        """`24` (the pre-ISSUE_84 shape) means a one-rung ladder — existing overrides keep working."""
        return [value] if isinstance(value, (int, float)) else value


class StallWatchdogConfig(BaseModel):
    """Worker liveness watchdog (ISSUE_75) — the engine must say when it stops working.

    On 2026-08-01 every worker stood still for nine days and nothing raised its voice: the
    process was alive, the API answered, the dashboard refreshed, the weekly cron fired. Only the
    work was gone. Detection latency was up to seven days (the weekly report's `STALE` marker).

    A worker is stalled when no pass has *completed* within `max(factor x cadence, floor_minutes)`.
    The floor carries the decision: the ingest cadence is 15s, so a bare factor of 3 would fire
    after 45 seconds and cry at every merely slow pass. With the defaults an ingest worker is
    called out after 15 minutes and an eval worker (10-minute bar close) after 30.
    """
    enabled: bool = True
    factor: int = 3              # multiples of the worker's own cadence before it counts as stalled
    floor_minutes: int = 15      # never alert earlier than this, whatever the cadence
    check_interval_seconds: int = 60      # how often the watchdog looks; cheap, in-memory only


class DiagnosticsConfig(BaseModel):
    """Self-observation the engine keeps for its own sake (ISSUE_76).

    Not metrics for a report someone reads weekly — the record that makes a *later* investigation
    read an instrument instead of guessing. ISSUE_73 shipped a hand-picked 10s fetch timeout with
    nothing to judge it by; when ecb_press timed out on 2026-08-15 the engine could not say whether
    the feed had been slow or dead, because a failed fetch left no trace of how long it took.

    The poll journal (`source_poll_log`) is the answer, and it is cheap: ~26k rows/day at the
    current cadence, ~60-70 MB at the default retention, one INSERT next to the `source_health`
    UPSERT already made per poll. `poll_log_enabled` is the kill switch all the same — diagnostics
    are worth paying for, not worth being unable to switch off.
    """
    poll_log_enabled: bool = True
    poll_log_retention_days: int = 14    # pruned once per UTC day by the writer
    # A feed whose p99 sits within this fraction of its timeout is flagged for review: it is not
    # failing yet, but it is close enough that a slow day would make it fail. 0.7 = warn from 7s
    # against the 10s default, which leaves room to react before the quarantine does it for us.
    timeout_warn_ratio: float = 0.7
    # Process-resource gauge (ISSUE_89). On 2026-08-01 the frozen process showed 5 sockets in
    # CLOSE_WAIT and 1,191 MB resident memory; neither was recorded anywhere, and the restart took
    # the measurement with it. Sampled on the stall-watchdog tick (60s) into `resource_samples`.
    resource_gauge_enabled: bool = True
    resource_retention_days: int = 14        # same window as the poll journal and the file log
    # RSS ceiling that warns once when crossed. 0 = off, and that is the shipped default on
    # purpose: the only datapoint is 1,191 MB on a *frozen* process, which is not a baseline. The
    # weekly line is what produces the number to set this from — guessing one now would be the
    # same mistake as moving a retrieval floor on a single window.
    resource_rss_warn_mb: int = 0


class TelegramConfig(BaseModel):
    """Telegram delivery channel (ISSUE_27) — the operator's alert surface.

    `bot_token` and `chat_id` are credentials: they belong in the gitignored
    `user_configs/app_config.json` override, never in the tracked file (which carries
    empty placeholders). The bot only ever reacts to `chat_id` — commands from any
    other chat are ignored.

    Sending and command-polling are separate switches. `enabled` governs *sending* (the
    weekly report, `/report` replies); `commands_enabled` additionally runs the long-poll
    command loop. Telegram allows only **one** getUpdates consumer per bot, so a bot
    shared with another service (e.g. the data collector polling the same token) must keep
    `commands_enabled = false` here — otherwise both poll and each gets `409 Conflict`.
    Chat-triggered reports on a shared bot need a *dedicated* bot for this engine.
    """
    enabled: bool = False              # master switch: any Telegram send (weekly report)
    bot_token: str = ''                # secret — set via user_configs override
    chat_id: str = ''                  # secret — the one chat the bot serves
    poll_interval_seconds: int = 30    # long-poll timeout for the command loop
    commands_enabled: bool = False     # run the command poller (needs a bot ONLY this
                                       # process polls — see the class docstring)
    report_command: str = '/report'    # the command that triggers an on-demand report


class WeeklyReportConfig(BaseModel):
    """Weekly report schedule (ISSUE_27) — cron fields for the APScheduler job.

    Structured fields (not a raw cron string) so Pydantic validates them; they map 1:1
    onto APScheduler's CronTrigger. Requires `telegram.enabled` to actually deliver.
    """
    enabled: bool = False
    day_of_week: str = 'sun'           # CronTrigger day_of_week (mon..sun)
    hour: int = 18
    minute: int = 0
    timezone: str = 'UTC'
    # Alongside each weekly report, dump the closed-day JSONL archive (ISSUE_13 export path).
    # All closed buckets are (re)written idempotently — whole buckets only, so it stays
    # byte-identical to a manual `export_cli` run. Default on; `report_cli --no-export` skips it.
    export_outcomes: bool = True
    export_dir: str = 'data/signal_export'   # archive root: <dir>/<stream_id>/<bucket>.jsonl


class AppConfig(BaseModel):
    version: str = '0.3.3'
    schema_version: str = '1.0'
    # Human names for the journals this engine may write into, keyed by `journal_id` — the
    # 12-char fingerprint of the database's own `system_identifier` (ISSUE_9). Reported on
    # /v1/health as `environment`; a fingerprint with no entry reports `unknown`.
    #
    # A mapping rather than a plain `environment: 'production'` string, and the difference is the
    # whole point. A free-standing label claims something about *this process*, and nothing checks
    # it — carry the config to another machine and it still says production. A label **keyed on the
    # journal's identity** claims something about a *specific database*: boot against a different
    # one and the fingerprint matches no entry, so the answer degrades to `unknown` on its own. The
    # misconfiguration announces itself instead of being inherited.
    #
    # Tracked config and this default carry ONE INERT EXAMPLE, so the shape is visible without
    # having to find the documentation. `EXAMPLE_ID` is not twelve lowercase hex characters and can
    # therefore never match a real fingerprint — it resolves nothing and names nothing.
    #
    # Real entries belong in each machine's gitignored `user_configs/app_config.json`, never here: a
    # fingerprint is a per-deployment fact, and a tracked mapping would be inherited by every fork
    # and every second instance — which would hand them a 'production' label for a database that is
    # not. That is the failure this whole design prevents, re-introduced through the config file.
    # A fresh checkout therefore names nothing and claims nothing: an unmapped journal reads
    # `unknown`, which is loud and fixable, where a wrongly inherited name would be silent.
    #
    # See `docs/development/diagnostics.md` — "Which instance am I looking at?" for the SQL that
    # reads a deployment's fingerprint.
    journal_names: Dict[str, str] = Field(
        default_factory=lambda: {'EXAMPLE_ID': 'example-only — map real ids in user_configs'})
    api: ApiConfig = Field(default_factory=ApiConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    log_level: str = 'INFO'
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    # Process-wide fallback socket deadline in seconds (ISSUE_73), applied at server boot. Python's
    # own default is `None` = block forever; this bounds any socket nobody gave an explicit timeout.
    # Deliberately looser than the feed timeout — it guards unknown callers, so it errs towards
    # never interrupting a legitimately slow one. The feed path does not rely on it.
    socket_default_timeout_seconds: int = 30
    # Wall-clock deadline for one worker pass (ISSUE_74). A pass that overruns it is abandoned and
    # the worker resumes on its next tick instead of staying dead until a restart. ~15x the slowest
    # pass observed in production (eval ~18s), and deliberately BELOW the stall watchdog's floor:
    # the engine gets a chance to heal itself before it raises its voice. Note the trade — the
    # abandoned *thread* keeps running (a blocked thread cannot be cancelled), so this bounds the
    # damage rather than undoing it; ISSUE_73 removed the known cause of such a hang.
    pass_timeout_seconds: int = 300
    source_health: SourceHealthConfig = Field(default_factory=SourceHealthConfig)
    diagnostics: DiagnosticsConfig = Field(default_factory=DiagnosticsConfig)
    stall_watchdog: StallWatchdogConfig = Field(default_factory=StallWatchdogConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    weekly_report: WeeklyReportConfig = Field(default_factory=WeeklyReportConfig)
    # Per-report defaults a call may override (ISSUE_104) — see report_config_types.
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
