"""Pydantic config schema for a single pipeline ("constellation JSON").

One file in configs/pipelines/ maps to one PipelineConfig: inputs (sources),
scope (market + symbols), retrieval params, trigger, and the breaking-news gate.
"""
import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from finiexragengine.utils.timeframe import TIMEFRAMES, timeframe_minutes


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
    # `type` is the pull-vs-event-socket axis (unchanged). Cadence is expressed differently
    # per worker: an eval trigger declares a `timeframe` (bar-close aligned, ISSUE_timeframe);
    # an ingest trigger keeps a relative `interval_seconds` (corpus refresh has no bar).
    type: Literal['interval', 'event'] = 'interval'
    timeframe: Optional[str] = None        # eval cadence: 'M1'..'D1' (see utils/timeframe.py)
    interval_seconds: int = 600            # ingest cadence in seconds; eval ignores it

    @model_validator(mode='after')
    def _validate_timeframe(self) -> 'TriggerConfig':
        # Reject an unknown frame at load, before a worker is built — fail fast, like the
        # model allowlist. None is allowed here (the ingest path has no timeframe); the
        # eval-trigger builder is what requires one.
        if self.timeframe is not None and self.timeframe not in TIMEFRAMES:
            raise ValueError(
                f'unknown trigger.timeframe {self.timeframe!r} — '
                f'supported: {", ".join(TIMEFRAMES)}')
        return self

    @property
    def cadence_seconds(self) -> int:
        """Effective cadence in seconds — the one place the two knobs collapse to a number.

        A `timeframe` (eval, bar-close) wins and is converted from minutes; otherwise the raw
        `interval_seconds` (ingest, relative). Cost projection, staleness and /health all read
        this so a non-M10 pipeline is not mis-projected against the stale 600s default.
        """
        if self.timeframe is not None:
            return timeframe_minutes(self.timeframe) * 60
        return self.interval_seconds


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
    # (query<->article). The cut is query-length dependent (coverage report 2026-07-19):
    # short symbol queries ("Bitcoin BTC") embed further from articles — on-topic lands
    # ~0.60-0.66, generic ~0.70+ → crypto constellation uses 0.68; long specific queries
    # (forex) land ~0.37-0.46 → 0.55 (this default) holds there.
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


class SymbolSpec(BaseModel):
    """One evaluated instrument (ISSUE_70): its ticker `key`, the pair's `base`/`quote` legs, and
    the readable retrieval `query`.

    `key` is the ticker (e.g. `BTCUSD`); `base`/`quote` are the split (`BTC`/`USD`), emitted
    downstream as `base_currency`/`quote_currency` so a consumer sees the pair without its own
    lookup. `query` is the readable asset name the prompt + retrieval use (falls back to `key`);
    it is engine-internal, never emitted. `enabled: false` switches the symbol off for the whole
    pipeline (like a disabled model variant) — a user override can toggle one symbol by `key`
    (merge-by-id) without restating the list.
    """
    key: str
    base: str
    quote: str
    query: Optional[str] = None
    enabled: bool = True

    @model_validator(mode='after')
    def _key_is_base_plus_quote(self) -> 'SymbolSpec':
        # Integrity gate at load (fail-fast, like the timeframe/model validators): the ticker must
        # be exactly base+quote — a typo (ETH/EURO, wrong quote) is a config error, never silent.
        if self.key != self.base + self.quote:
            raise ValueError(
                f"symbol key '{self.key}' must equal base+quote "
                f"('{self.base}' + '{self.quote}' = '{self.base + self.quote}')")
        return self

    def retrieval_query(self) -> str:
        """The prompt/retrieval subject — the readable query, or the key when unset."""
        return self.query or self.key


class PipelineConfig(BaseModel):
    pipeline_id: str
    outcome_type: str
    market: str
    symbols: List[SymbolSpec]                                      # evaluated instruments (ISSUE_70)
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

    def active_symbols(self) -> List['SymbolSpec']:
        """The enabled symbols in declared order — a disabled one is off for the whole pipeline."""
        return [spec for spec in self.symbols if spec.enabled]

    def symbol_keys(self) -> List[str]:
        """The active ticker keys in declared order — for surfaces that list symbols (ISSUE_70)."""
        return [spec.key for spec in self.active_symbols()]

    def symbol_query_map(self) -> Dict[str, str]:
        """`{key: retrieval_query}` over the active symbols — for retrieval/coverage callers (ISSUE_70)."""
        return {spec.key: spec.retrieval_query() for spec in self.active_symbols()}
