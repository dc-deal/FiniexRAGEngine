"""API-facing response models (Pydantic — required for FastAPI serialization)."""
from typing import List

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str = 'ok'
    service: str = 'FiniexRAGEngine'
    version: str


class PipelineInfo(BaseModel):
    pipeline_id: str
    outcome_type: str
    market: str
    symbols: List[str]
    trigger_type: str


class PipelinesResponse(BaseModel):
    pipelines: List[PipelineInfo]
