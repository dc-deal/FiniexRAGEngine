"""API-facing response models (Pydantic — required for FastAPI serialization)."""
from datetime import datetime
from typing import List, Optional, Tuple

from pydantic import BaseModel


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
    # of them unread. `None` when the trigger carries no timeframe.
    #
    # Exposed because a consumer's staleness contract is derived from it: silence longer than one
    # cadence is what tells them the producer stopped, so the threshold that blocks their order
    # entry rested on a hand-copied constant. Note the direction that makes it usable — an
    # out-of-band pass makes the OBSERVED interval shorter than nominal, never longer, so the
    # cadence is an upper bound on normal quiet.
    cadence_seconds: Optional[int] = None


class PipelinesResponse(BaseModel):
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
