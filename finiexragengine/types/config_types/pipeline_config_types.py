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


class PipelineLlmConfig(BaseModel):
    """The pipeline's evaluation model — REQUIRED, never inherited from a global default.

    The model is series-defining, exactly like the prompt (ISSUE_33): a different model
    yields different scores for the same news. Requiring it here keeps the choice
    deliberate and per-flow — a global config edit can never silently retarget every
    pipeline's series. Must be inside `app_config.llm.allowed_models` (checked at
    assembly). Accepts fine-tune ids (`ft:...`) once they are allowlisted.
    """
    model: str


class PromptRef(BaseModel):
    """The prompt a pipeline uses — its template `name` and `version` (ISSUE_33).

    Resolves to `prompts/<name>_v<version>.md`; the template's front-matter carries the
    stable id + content hash recorded with every outcome. Each pipeline declares its own,
    so prompts are swappable per constellation without touching code.
    """
    name: str = 'sentiment'
    version: str = '1'


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
    # Relevance floor (ISSUE_24): candidates whose query<->article cosine *distance*
    # (pgvector `<=>`, = 1 - similarity) exceeds this are off-topic and dropped — an
    # empty context becomes the mechanical no_data HOLD instead of a paid LLM call on
    # generic articles. None disables the floor. Note the axis: dedup_similarity cuts
    # what is too similar (article<->article), the floor cuts what is too dissimilar
    # (query<->article). 0.55 tuned on the crypto corpus (coverage report).
    floor_distance: Optional[float] = 0.55
    deep_tier: Optional[DeepTierConfig] = None   # None = recent-only (sentiment default, ISSUE_5)


class BreakingConfig(BaseModel):
    urgency_threshold: float = 0.8       # push gate for breaking news (ISSUE_6)


class PipelineConfig(BaseModel):
    pipeline_id: str
    outcome_type: str
    market: str
    symbols: List[str]
    symbol_queries: Dict[str, str] = Field(default_factory=dict)   # symbol → retrieval query text (ISSUE_5)
    prompt: PromptRef = Field(default_factory=PromptRef)           # declared prompt template (ISSUE_33)
    llm: PipelineLlmConfig                                         # declared eval model — required
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    sources: List[SourceConfig]
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    breaking: BreakingConfig = Field(default_factory=BreakingConfig)
