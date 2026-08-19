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


class PipelinesResponse(BaseModel):
    pipelines: List[PipelineInfo]
