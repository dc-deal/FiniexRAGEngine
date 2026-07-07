"""Pydantic config schema for a single pipeline ("constellation JSON").

One file in configs/pipelines/ maps to one PipelineConfig: inputs (sources),
scope (market + symbols), retrieval params, trigger, and the breaking-news gate.
"""
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SourceConfig(BaseModel):
    source_id: str
    type: Literal['rss', 'blog', 'socket', 'api'] = 'rss'
    url: str
    weight: float = 1.0          # source trust / weight (ISSUE_5)


class TriggerConfig(BaseModel):
    type: Literal['interval', 'event'] = 'interval'
    interval_seconds: int = 600


class DeepTierConfig(BaseModel):
    """Opt-in second retrieval tier: older articles gated by importance (ISSUE_5)."""
    min_importance: int = 2
    window_minutes: int = 43200          # how far back the deep tier may reach (30 days)


class RetrievalConfig(BaseModel):
    top_k: int = 12
    recency_window_minutes: int = 1440   # recency window for retrieval (ISSUE_3)
    dedup_similarity: float = 0.92       # pairwise cosine >= this collapses near-duplicates (ISSUE_5)
    deep_tier: Optional[DeepTierConfig] = None   # None = recent-only (sentiment default, ISSUE_5)


class BreakingConfig(BaseModel):
    urgency_threshold: float = 0.8       # push gate for breaking news (ISSUE_6)


class PipelineConfig(BaseModel):
    pipeline_id: str
    outcome_type: str
    market: str
    symbols: List[str]
    symbol_queries: Dict[str, str] = Field(default_factory=dict)   # symbol → retrieval query text (ISSUE_5)
    prompt_version: str = '1'
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    sources: List[SourceConfig]
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    breaking: BreakingConfig = Field(default_factory=BreakingConfig)
