"""Outcome models — the generic response envelope plus per-pipeline payloads.

These are Pydantic models because they are serialized identically to every
surface: the collector's JSONL archive, the live worker, and the HTTP API.
"""
from datetime import datetime
from typing import Generic, List, Literal, TypeVar

from pydantic import BaseModel, Field


class ArticleRef(BaseModel):
    """Provenance pointer to a source article that fed an outcome (ISSUE_2)."""
    article_id: str
    url: str
    title: str
    published_at: datetime


class StageTiming(BaseModel):
    """Per-stage timing record (ISSUE_7) — debug + IDE signal alignment.

    stage: one of 'fetch' | 'embed' | 'retrieve' | 'llm' | 'parse'.
    """
    stage: str
    started_at: datetime
    ended_at: datetime
    duration_ms: float


class SentimentResult(BaseModel):
    """Per-symbol sentiment outcome — the first outcome_type payload.

    Future outcome types (long-term trend, currency events) add their own
    result model; the envelope below is generic over the payload type.
    """
    symbol: str
    signal: Literal['BUY', 'SELL', 'HOLD']
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    urgency: float = Field(default=0.0, ge=0.0, le=1.0)   # breaking-news gate (ISSUE_6)
    is_breaking: bool = False
    sources: List[ArticleRef] = Field(default_factory=list)  # provenance (ISSUE_2)


class RunError(BaseModel):
    type: str
    message: str
    timestamp: datetime


class RunMetadata(BaseModel):
    """What happened internally during a run — debugging + data-quality."""
    model: str
    sources_configured: int = 0
    sources_reached: int = 0
    articles_found: int = 0
    articles_relevant: int = 0
    processing_time_ms: float = 0.0
    stage_timings: List[StageTiming] = Field(default_factory=list)  # ISSUE_7


T = TypeVar('T')


class AnalysisEnvelope(BaseModel, Generic[T]):
    """Generic response envelope — common shell + per-pipeline payload.

    The `result` payload type varies per outcome_type; the shell is identical
    across pipelines so every consumer (collector JSONL, live worker, API)
    parses the same structure.
    """
    schema_version: str = '1.0'
    pipeline_id: str
    outcome_type: str
    prompt_version: str
    timestamp: datetime
    status: Literal['success', 'partial', 'error']
    result: List[T] = Field(default_factory=list)
    metadata: RunMetadata
    errors: List[RunError] = Field(default_factory=list)


# First concrete outcome type. Future: TrendEnvelope, CurrencyEventEnvelope, ...
SentimentEnvelope = AnalysisEnvelope[SentimentResult]
