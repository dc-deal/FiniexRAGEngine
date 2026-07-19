"""Pydantic config schema for a single pipeline ("constellation JSON").

One file in configs/pipelines/ maps to one PipelineConfig: inputs (sources),
scope (market + symbols), retrieval params, trigger, and the breaking-news gate.
"""
import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class LlmVariant(BaseModel):
    """One model variant of a fanned constellation (ISSUE_42).

    `sub_pipeline_id` names the *stream*, decoupled from the model behind it: the model
    can be swapped or pinned (alias → dated snapshot, ISSUE_40) without renaming the
    series. Charset `[a-z0-9_]` keeps derived stream ids path/collector-safe.
    """
    name: str                    # the model id — allowlist-gated at assembly (ISSUE_40)
    sub_pipeline_id: str
    default: bool = False        # exactly one variant keeps the bare pipeline_id
    enabled: bool = True         # a disabled variant stays defined but is not expanded/run


class PipelineLlmConfig(BaseModel):
    """The pipeline's evaluation model(s) — REQUIRED, never inherited from a global default.

    The model is series-defining, exactly like the prompt (ISSUE_33): a different model
    yields different scores for the same news. Requiring the declaration here keeps the
    choice deliberate and per-flow — a global config edit can never silently retarget
    every pipeline's series. Exactly one form:

    - `model` — the single-model constellation (one stream, today's default), or
    - `models` — the variant fan-out (ISSUE_42): the registry expands the constellation
      into one logical pipeline per variant; the `default` variant keeps the bare
      `pipeline_id`, the others get `<pipeline_id>_<sub_pipeline_id>`.

    Every named model must be inside `app_config.llm.allowed_models` (checked at
    assembly). Accepts fine-tune ids (`ft:...`) once they are allowlisted.
    """
    model: Optional[str] = None
    models: Optional[List[LlmVariant]] = None

    @model_validator(mode='after')
    def _exactly_one_form(self) -> 'PipelineLlmConfig':
        if (self.model is None) == (self.models is None):
            raise ValueError("declare exactly one of llm.model (single) or "
                             'llm.models (variant fan-out, ISSUE_42)')
        if self.models is not None:
            if sum(1 for variant in self.models if variant.default) != 1:
                raise ValueError('llm.models needs exactly one variant with default: true '
                                 '(it keeps the bare pipeline_id)')
            # A disabled variant is skipped at expansion (kept defined, not run) — but the
            # default owns the bare pipeline_id, so disabling it would leave no default stream.
            if not next(variant for variant in self.models if variant.default).enabled:
                raise ValueError('the default variant cannot be disabled (enabled: false) — it '
                                 'keeps the bare pipeline_id; disable a non-default variant instead')
            sub_ids = [variant.sub_pipeline_id for variant in self.models]
            if len(set(sub_ids)) != len(sub_ids):
                raise ValueError(f'llm.models sub_pipeline_ids must be unique: {sub_ids}')
            for sub_id in sub_ids:
                if not re.fullmatch(r'[a-z0-9_]+', sub_id):
                    raise ValueError(f"sub_pipeline_id '{sub_id}' must match [a-z0-9_]+ "
                                     '(stream ids stay path/collector-safe)')
        return self


class PromptRef(BaseModel):
    """The prompt a pipeline uses — its template `name` and `version` (ISSUE_33).

    Resolves to `prompts/<name>/<name>_v<version>.md` (one folder per prompt family);
    the template's front-matter carries the stable id + content hash recorded with
    every outcome. Each pipeline declares its own, so prompts are swappable per
    constellation without touching code.
    """
    name: str = 'crypto_sentiment'
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
    """Per-pipeline breaking gates — two knobs at two stages of the funnel (ISSUE_11).

    Detection flagging is one shared write on the corpus (source-set-scoped); *sensitivity* is
    per-pipeline, so the same corpus can wake an eager and a conservative pipeline differently:

    - `min_importance` — the **wake** gate: which detected importance tier (1=LOW/2=MID/3=HIGH)
      is hot enough to run *this* pipeline's eval out-of-band. Answers "is it worth paying to look
      now?" — a cheap-side knob, before any LLM spend.
    - `urgency_threshold` — the **confirm** gate: `is_breaking = urgency >= this` on the LLM's own
      score. Answers "having read it, is it market-moving enough to count as breaking?" — after the
      LLM read it. The two are orthogonal on purpose (see docs/architecture).
    """
    urgency_threshold: float = 0.8       # push gate for breaking news (ISSUE_6)
    min_importance: int = 2              # wake sensitivity: MID+ clusters wake this pipeline (ISSUE_11)


class OutputGuardConfig(BaseModel):
    """Output consistency guard tolerances (ISSUE_35) — the semantic layer over the schema.

    Schema validation (`SentimentLlmOutput`) proves a completion is well-formed and in
    range; these knobs bound what still counts as *coherent*. Per-pipeline like
    `retrieval`/`breaking`: score semantics differ per prompt family, so another
    constellation may tolerate different contradictions.

    - `score_signal_tolerance` — dead zone around 0 for the signal<->score rule: a BUY
      passes while `sentiment_score >= -this` (SELL mirrored). 0 degrades every wobble
      around zero; 1 disables the rule.
    - `hold_confidence_max` — the highest `confidence` a no-signal HOLD may carry; above
      it the row reads as a degenerate completion, not a judgement. 1 disables the rule.
    """
    score_signal_tolerance: float = 0.1
    hold_confidence_max: float = 0.9


class PipelineConfig(BaseModel):
    pipeline_id: str
    outcome_type: str
    market: str
    symbols: List[str]
    symbol_queries: Dict[str, str] = Field(default_factory=dict)   # symbol → retrieval query text (ISSUE_5)
    prompt: PromptRef = Field(default_factory=PromptRef)           # declared prompt template (ISSUE_33)
    llm: PipelineLlmConfig                                         # declared eval model — required
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)   # EVAL cadence (ISSUE_10)
    # The shared feed group this pipeline evaluates over (ISSUE_10) — a reference into
    # configs/source_sets/<id>.json, resolved at assembly. Constellations never own
    # feeds; acquisition (sources + ingest cadence) is the source-set's concern.
    source_set: str
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    breaking: BreakingConfig = Field(default_factory=BreakingConfig)
    output_guard: OutputGuardConfig = Field(default_factory=OutputGuardConfig)   # coherence tolerances (ISSUE_35)
    # Variant provenance (ISSUE_42) — set ONLY by the registry's fan-out expansion,
    # never in a constellation file: which constellation this stream derives from
    # (`variant_group` = the default stream's id) and which variant it is.
    variant_group: Optional[str] = None
    variant: Optional[str] = None
