"""Outcome models — the generic response envelope plus per-pipeline payloads.

These are Pydantic models because they are serialized identically to every
surface: the collector's JSONL archive, the live worker, and the HTTP API.
"""
from datetime import datetime
from typing import Dict, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_serializer


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
    # How this row came to be (ISSUE_24/35) — machine-readable, filterable downstream:
    # 'llm' = scored by the model · 'no_data' = mechanical HOLD, retrieval empty after the
    # floor (no evaluation possible due to data shortage — no LLM call was made) ·
    # 'degraded' = a guard/failure degraded the row. Additive with default: old envelopes
    # stay parseable, schema_version is unchanged.
    basis: Literal['llm', 'no_data', 'degraded'] = 'llm'


class SentimentLlmOutput(BaseModel):
    """The scored fields the LLM must return for one symbol (ISSUE_6).

    A strict subset of SentimentResult: the model scores the mood; provenance
    (`sources`), `is_breaking` and `symbol` are attached by the engine, never invented
    by the LLM. All fields required + no extras (`forbid`), so it maps cleanly to a
    JSON schema and rejects a malformed completion.
    """
    model_config = ConfigDict(extra='forbid')

    signal: Literal['BUY', 'SELL', 'HOLD']
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    urgency: float = Field(ge=0.0, le=1.0)


class RunError(BaseModel):
    type: str
    message: str
    timestamp: datetime


class RunMetadata(BaseModel):
    """What happened internally during a run — debugging + data-quality."""
    model: str
    # The model the API actually *served* (response.model, dated snapshot) — the
    # configured `model` is an alias the provider can silently retarget; this field
    # makes such a switch visible in the series (the model-side prompt_hash, ISSUE_33).
    model_snapshot: str = ''
    sources_configured: int = 0
    sources_reached: int = 0
    articles_found: int = 0
    articles_relevant: int = 0
    processing_time_ms: float = 0.0
    stage_timings: List[StageTiming] = Field(default_factory=list)  # ISSUE_7
    # Run-level spend capture (ISSUE_12, assembled in ISSUE_7): summed LLM usage, the
    # run's total derived USD (embeddings + LLM), and per-symbol token footprints.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    per_symbol_tokens: Dict[str, int] = Field(default_factory=dict)
    # Variant grouping hints (ISSUE_42, additive — confirmed with the Testing IDE):
    # present only on streams of a fanned constellation. `variant_group` = the default
    # stream's pipeline_id ("this series derives from that one"); `variant` = this
    # stream's sub id. `pipeline_id == variant_group` ⇔ the default variant. A consumer
    # groups fan streams by these instead of parsing stream ids.
    variant_group: Optional[str] = None
    variant: Optional[str] = None

    @model_serializer(mode='wrap')
    def _omit_absent_hints(self, handler):
        # Single-model pipelines omit the hint keys entirely (absent = today's JSON,
        # no schema bump) instead of serializing nulls.
        data = handler(self)
        for key in ('variant_group', 'variant'):
            if data.get(key) is None:
                data.pop(key, None)
        return data


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
    # Prompt provenance (ISSUE_33): `prompt_id` + `prompt_version` name the prompt series;
    # `prompt_hash` fingerprints the template body so a silent edit is visible downstream.
    # Populated from PromptMetadata when the envelope is assembled (ISSUE_7); default '' keeps
    # older archived envelopes (pre-ISSUE_33) parseable.
    prompt_version: str
    prompt_id: str = ''
    prompt_hash: str = ''
    timestamp: datetime
    status: Literal['success', 'partial', 'error']
    result: List[T] = Field(default_factory=list)
    metadata: RunMetadata
    errors: List[RunError] = Field(default_factory=list)


# First concrete outcome type. Future: TrendEnvelope, CurrencyEventEnvelope, ...
SentimentEnvelope = AnalysisEnvelope[SentimentResult]
