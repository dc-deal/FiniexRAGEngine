"""API-facing response models (Pydantic — required for FastAPI serialization)."""
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class WorkerInfo(BaseModel):
    """One background worker's live status (ISSUE_10) — mirrored from WorkerState."""
    name: str
    kind: str
    interval_seconds: int
    runs: int
    last_status: str
    last_run_at: Optional[datetime] = None
    last_duration_ms: float = 0.0
    last_detail: str = ''
    # Populated only for a worker whose task ended unexpectedly — the pair that turns a silently
    # dead worker into something /health states outright rather than implying via a stale `last`.
    stopped_at: Optional[datetime] = None
    stopped_reason: str = ''


class BudgetInfo(BaseModel):
    """Cost circuit-breaker state (ISSUE_47) — is paid work suspended, and until when."""
    enabled: bool = True
    suspended: bool = False
    reason: Optional[str] = None
    retry_at: Optional[str] = None
    day_spend_usd: float = 0.0
    soft_daily_usd: float = 0.0


class StallInfo(BaseModel):
    """Worker liveness state (ISSUE_75) — which workers have gone silent, and on what threshold.

    Deliberately part of /health rather than a separate endpoint: "is anything stuck" is the
    question /health exists to answer, and in August 2026 it was the one question no surface
    could answer without a stack dump.
    """
    enabled: bool = True
    stalled: List[str] = []
    factor: int = 3
    floor_minutes: int = 15


class ResourceInfo(BaseModel):
    """Process resource state (ISSUE_89) — is this process growing?

    Served from the gauge's **live sample**, never from `resource_samples`: a health endpoint that
    depends on a diagnostic table being reachable answers the wrong question when the database is
    the thing that broke. The table is the series; this is the moment.

    Every field is optional because the gauge degrades rather than failing — a missing `psutil`
    disables it, and a platform that refuses a socket count (Windows) nulls that one field.
    """
    enabled: bool = True
    rss_mb: Optional[float] = None
    open_sockets: Optional[int] = None
    threads: Optional[int] = None
    sampled_at: Optional[str] = None
    ceiling_mb: int = 0
    over_ceiling: bool = False


HEALTH_STATUSES: Tuple[str, ...] = ('ok', 'degraded')


class HealthResponse(BaseModel):
    # 'ok' | 'degraded'. A plain str with the domain kept as data (CLAUDE.md): a monitor polling
    # this endpoint must keep parsing a value a later version introduces. It was a hardcoded 'ok'
    # until 2026-08-22 — including for the 37 hours an ingest worker lay dead, which is exactly the
    # window an external check exists to catch.
    status: str = 'ok'
    service: str = 'FiniexRAGEngine'
    version: str
    # The deadline a single pass is abandoned at (ISSUE_74). Engine-level and NOT repeated under
    # each pipeline, deliberately: it is one number for every worker, and a per-pipeline copy would
    # claim to be a per-stream property. Someone would eventually set two of them differently, and
    # the engine would honour neither the second value nor the reader's expectation. When it truly
    # becomes per-pipeline, moving it is a visible contract change rather than a stable name
    # quietly changing meaning.
    #
    # A consumer needs it because it bounds how far an out-of-band pass can overtake a scheduled
    # one: past this deadline the pass is abandoned and produces nothing, so `seq` can lead
    # evidence by at most one of these (ISSUE_9 RC-4).
    pass_timeout_seconds: int
    # Which journal this engine writes into — a 12-char fingerprint of the database's own identifier
    # (ISSUE_9). Derived, never configured: a declared environment label is a claim, and a
    # mislabelled dev instance would make a rehearsal look like proof. `None` when the identifier is
    # unreadable (managed Postgres) or no store is attached (scaffold-mock mode).
    journal_id: Optional[str] = None
    # The human name for the journal above, resolved through `journal_names` in the configuration
    # (ISSUE_9). `unknown` when the fingerprint has no entry — or when there is no fingerprint to
    # look up at all. Because the name is keyed on the journal's identity, a configuration carried
    # to a different database cannot carry its label with it: the lookup simply misses.
    environment: str = 'unknown'
    # Empty when the server runs without --workers (API-only mode, no background spend).
    workers: List[WorkerInfo] = []
    # Present only with real runners attached (the guard lives on the assembler, ISSUE_47).
    budget: Optional[BudgetInfo] = None
    # Present only with workers running — nothing can stall without them (ISSUE_75).
    stall: Optional[StallInfo] = None
    # Present only with workers running — the gauge rides the watchdog's tick (ISSUE_89).
    resources: Optional[ResourceInfo] = None


class PipelineInfo(BaseModel):
    pipeline_id: str
    outcome_type: str
    market: str
    symbols: List[str]
    trigger_type: str
    # The eval cadence in SECONDS, not as the `M10` token (ISSUE_9). A consumer computes a staleness
    # threshold with the number; the token is a rendering of it, and shipping both would leave one
    # of them unread.
    #
    # Exposed because a consumer's staleness contract is derived from it: silence longer than one
    # cadence is what tells them the producer stopped, so the threshold that blocks their order
    # entry rested on a hand-copied constant. Note the direction that makes it usable — an
    # out-of-band pass makes the OBSERVED interval shorter than nominal, never longer, so the
    # cadence is an upper bound on normal quiet.
    #
    # Always present, and no longer `Optional`: it is `TriggerConfig.cadence_seconds`, which
    # resolves a timeframe first and falls back to the raw interval, so there is no configuration
    # that yields nothing. The nullable form went with the router's own second derivation of this
    # number — and a field documented as "absent when …" that can no longer be absent is exactly
    # the stale claim a reader plans around.
    cadence_seconds: int


class StreamInfo(BaseModel):
    """The stream's engine-wide numbers, served once per response (ISSUE_9).

    **Response level, not on each pipeline row** — and the placement is the point. Both values are
    properties of the engine, so a copy per pipeline would claim to be a per-stream property;
    someone would eventually set two of them differently and the engine would honour neither the
    second value nor the reader's expectation. `pass_timeout_seconds` on `/health` is engine-level
    for exactly this reason. If either ever becomes genuinely per-stream it moves to the row, which
    is a visible contract change rather than a stable name quietly changing meaning.

    Both fields are **required**. The consumer asked to depend on their presence: they intend to let
    the served value govern and keep only their own multiple (a 3x connection watchdog), so a null
    here would put a branch in their code for a state the engine cannot be in.
    """
    # Keep-alive cadence, so their watchdog is read rather than hand-copied — a change on our side
    # would otherwise arrive as a false outage.
    heartbeat_seconds: int
    # How far back `?since=`/`?history=N` may reach on the stream and the range endpoint.
    replay_window_hours: int


class PipelinesResponse(BaseModel):
    # Engine-wide facts about the transport (ISSUE_9). Every value that varies per stream lives on
    # the row below; everything that has exactly one value lives here, once.
    stream: StreamInfo
    pipelines: List[PipelineInfo]


class BuildInfo(BaseModel):
    """Which code this process is running — the `/v1/build` payload (ISSUE_65 follow-up).

    Separate from `/health` on purpose: health describes **state** and changes every second, this
    describes **identity** and is constant for the process's lifetime. Keeping them apart also
    leaves `/health`'s documented contract untouched, which a consumer depends on.

    Every field is optional because none of it may ever be worth a boot failure: a deployment
    without git (a container image, an unpacked archive) answers `null` rather than refusing to
    start. `null` therefore means "not determinable here", never "unknown version".
    """
    # The release string from app_config — moves only when a batch ships, so between two tags every
    # deploy looks identical. That gap is the reason the fields below exist.
    version: str
    commit: Optional[str] = None          # short hash, sampled ONCE at startup
    committed_at: Optional[datetime] = None
    # True when the working tree had uncommitted changes at startup. On a server reached by RDP an
    # in-place edit is plausible, and this is the difference between "which deploy is live" and
    # "...and has anyone touched it".
    dirty: Optional[bool] = None
    # When this process started. Answers the question the hash cannot: did my restart take effect?
    started_at: datetime


class AppliedParamInfo(BaseModel):
    """One parameter as it was applied, and where it came from (ISSUE_104).

    Echoed rather than assumed. Two people comparing two answers can then see *why* they differ,
    instead of inferring it — the same lesson `SettingResolver` wrote down for boot settings, and
    the one this codebase has now relearned from a warn-only line that read as a spend cap, an
    exemption switch that removed a rate limit, and a day accumulator that resets on restart.
    """
    value: Any
    source: str                 # 'config' | 'request'
    clamped: bool = False       # true when a bound shortened what was asked for


class ReportEnvelope(BaseModel):
    """One report's payload plus what produced it."""
    report: str
    generated_at: datetime
    # Every parameter this report accepted, its applied value and its origin. Empty for a report
    # that takes none.
    params: Dict[str, AppliedParamInfo] = Field(default_factory=dict)
    # The window actually used, resolved from `params` — a convenience for the common case, never
    # a second source of truth.
    since: Optional[datetime] = None
    # The report's own shape, serialized by `utils.dataclass_json` — deliberately untyped here.
    # These are internal diagnostic shapes and must stay free to change; typing them would turn
    # every report row into an API contract, which is what the doc says this surface is not.
    data: Any


class ReportCatalogEntry(BaseModel):
    """One report as the catalog listing presents it."""
    name: str
    summary: str
    params: List[str]
    required: List[str]
    # The CONFIGURED defaults, so the listing and a call can never advertise different values.
    defaults: Dict[str, Any] = Field(default_factory=dict)


class ReportCatalog(BaseModel):
    reports: List[ReportCatalogEntry]
    max_window_days: int


class EnvelopeRange(BaseModel):
    """`GET /v1/pipelines/{id}/envelopes` — a bounded range of the series (ISSUE_9 §2).

    The collector's catch-up path, and the reason it exists rather than a flag on `/latest`:
    `/latest` returns only the newest envelope per pipeline, so everything produced between two polls
    that is no longer newest at poll time is never fetched — systematically the out-of-band breaking
    passes, which are overtaken by the next scheduled pass within one cadence period.

    **The mapping rule between this surface and the stream is worth stating once**: a condition that
    is *terminal* on the stream (`epoch_changed`, `cursor_ahead`) is a **409** here, because in both
    cases the caller's cursor is unusable and returning rows would be actively wrong. A condition
    that is a *non-terminal marker* on the stream (`replay_truncated`) is a **body field** here,
    because a truncated range still carries data the caller wants. Two renderings, one decision — the
    decision itself lives in `StreamReplay`.
    """
    pipeline_id: str
    # The epoch these envelopes belong to. Part of the archive key `(pipeline_id, stream_epoch, seq)`,
    # so a caller writing them down needs it even when it never changes.
    stream_epoch: int
    # The stream's current position, so a paging caller knows whether to ask again without guessing
    # from the row count.
    head_seq: int
    # The stored JSON, verbatim — never re-validated on the way out, for the same reason a stream
    # frame is not: a model default would rewrite an archived line and the parity claim would become
    # a claim about the model.
    envelopes: List[Dict[str, Any]] = Field(default_factory=list)
    # True when the requested `since` was older than `replay_window_hours`. Never silent: the field
    # below names the oldest position still held, so the caller learns exactly what it must fetch
    # from the journal export (#62) instead of discovering a hole later.
    truncated: bool = False
    oldest_available_seq: Optional[int] = None
