"""API-facing response models (Pydantic — required for FastAPI serialization)."""
from datetime import datetime
from typing import List, Optional

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


class HealthResponse(BaseModel):
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
