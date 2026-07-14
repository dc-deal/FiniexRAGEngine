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


class HealthResponse(BaseModel):
    status: str = 'ok'
    service: str = 'FiniexRAGEngine'
    version: str
    # Empty when the server runs without --workers (API-only mode, no background spend).
    workers: List[WorkerInfo] = []
    # Present only with real runners attached (the guard lives on the assembler, ISSUE_47).
    budget: Optional[BudgetInfo] = None


class PipelineInfo(BaseModel):
    pipeline_id: str
    outcome_type: str
    market: str
    symbols: List[str]
    trigger_type: str


class PipelinesResponse(BaseModel):
    pipelines: List[PipelineInfo]
