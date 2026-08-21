"""Outcome models — the generic response envelope plus per-pipeline payloads.

These are Pydantic models because they are serialized identically to every
surface: the collector's JSONL archive, the live worker, and the HTTP API.
"""
from datetime import datetime
from typing import Any, Dict, Generic, List, Literal, Optional, Tuple, TypeVar, get_args

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from pydantic.functional_serializers import SerializerFunctionWrapHandler


# --- closed vocabularies (ISSUE_94) --------------------------------------------------------
# Strict at the producing seam, permissive at the parsing boundary. The aliases below type the
# code that *builds* a row, so a typo fails where it is written; the model fields themselves are
# plain `str`, so an archived envelope carrying a value a later version introduced still loads.
# The envelope contract's "always parseable" rule outranks type strictness — a reader pinned to an
# older version must ignore an unknown tag, not refuse the line. The same split `TriggerReason`
# and `RunError.type` already use; these four were the ones that did not.
#
# The read path that decides it: on boot the breaking tracker validates every envelope of the last
# 72 h (`outcome_store.get_since`). One line carrying an unknown value would raise, the seed would
# return nothing, and the boot pass would re-open running stories as fresh episodes — the exact
# restart artefact ISSUE_82 removed, arriving by a different route.

# Not `Signal`: in a trading codebase that word already means signal data, a signal series and
# the consumer's SIGNAL worker. A grep for it returns everything, which is the same as nothing.
SentimentSignal = Literal['BUY', 'SELL', 'HOLD']
SENTIMENT_SIGNALS: Tuple[str, ...] = get_args(SentimentSignal)

# 'llm' = scored by the model · 'no_data' = mechanical HOLD, retrieval empty after the floor (no
# LLM call was made) · 'degraded' = a guard or failure produced the row.
ResultBasis = Literal['llm', 'no_data', 'degraded']
RESULT_BASES: Tuple[str, ...] = get_args(ResultBasis)

RunStatus = Literal['success', 'partial', 'error']
RUN_STATUSES: Tuple[str, ...] = get_args(RunStatus)

# Where the data came from. A naming convention could not carry this: the Testing IDE once found a
# generated week and a real week with byte-identical provenance.
DataOrigin = Literal['live', 'synthetic']
DATA_ORIGINS: Tuple[str, ...] = get_args(DataOrigin)


class ArticleRef(BaseModel):
    """Provenance pointer to a source article that fed an outcome (ISSUE_2)."""
    article_id: str
    url: str
    title: str
    published_at: datetime
    # When the engine fetched this source (ISSUE_11 reaction time: engine-reaction =
    # envelope timestamp − earliest source fetched_at). Additive with a default so
    # pre-ISSUE_11 archived envelopes stay parseable; always set on new envelopes. The
    # detection timestamp (flagged_at) is joined from the corpus by article_id at report time.
    fetched_at: Optional[datetime] = None


class StageTiming(BaseModel):
    """Per-stage timing record (ISSUE_7) — debug + IDE signal alignment.

    stage: one of 'fetch' | 'embed' | 'retrieve' | 'llm' | 'parse'.
    """
    stage: str
    started_at: datetime
    ended_at: datetime
    duration_ms: float


class RetrievalFunnel(BaseModel):
    """Per-query retrieval funnel counters (ISSUE_24) — how the prompt context came to be.

    Captured by the retriever as a byproduct of the squeeze and persisted with the
    envelope (`metadata.per_symbol_retrieval`), so a thin or empty context is explainable
    after the fact: was the window empty, or did the floor drop everything? Additive and
    non-load-bearing — never bumps `schema_version`.

    `best_distance`/`worst_distance` span the candidate distances *before* the floor
    (None when the window was empty); `floor` is the cut applied on this run — snapshot
    at the call, so a persisted envelope stays interpretable after a config retune.
    Together they place the floor inside the spread (the live calibration view).
    """
    in_window: int = 0        # candidates fetched inside the recency/deep windows
    floor_dropped: int = 0    # dropped as off-topic (distance > floor_distance)
    tier_duplicates: int = 0  # same article surfaced by both tiers
    near_duplicates: int = 0  # near-duplicate stories collapsed (>= dedup_similarity)
    kept: int = 0             # what reached the prompt (<= top_k)
    best_distance: Optional[float] = None    # nearest candidate pre-floor (nearest miss on 0 kept)
    worst_distance: Optional[float] = None   # farthest candidate pre-floor
    floor: Optional[float] = None            # floor_distance applied this run (None = disabled)


class SentimentResult(BaseModel):
    """Per-symbol sentiment outcome — the first outcome_type payload.

    Future outcome types (long-term trend, currency events) add their own
    result model; the envelope below is generic over the payload type.
    """
    symbol: str
    # `str`, not `SentimentSignal` — see the vocabulary note above: strict where a row is built, permissive
    # where an archived one is read.
    signal: str
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    urgency: float = Field(default=0.0, ge=0.0, le=1.0)   # breaking-news gate (ISSUE_6)
    is_breaking: bool = False
    sources: List[ArticleRef] = Field(default_factory=list)  # provenance (ISSUE_2)
    # How fresh the evidence behind this row is (ISSUE_9): max(fetched_at) across `sources`, in
    # epoch-ms UTC. Deliberately our *fetch* time, not the article's `published_at` — publication
    # time is publisher-controlled and backdatable, fetch time is on our clock and monotonic with
    # the ingest. **Absent means the row rests on no evidence at all**, which coincides with
    # `basis: 'no_data'` — never "unknown". It exists because `seq` orders *availability*, not
    # evidence freshness: since ISSUE_74 removed the shared pass lock, an out-of-band pass can
    # finish after a scheduled one, carry the higher `seq` and rest on older articles. A consumer
    # discounts such an envelope with this field; without it the flip-flop is invisible.
    evidence_as_of: Optional[int] = None
    # How this row came to be (ISSUE_24/35) — machine-readable, filterable downstream:
    # 'llm' = scored by the model · 'no_data' = mechanical HOLD, retrieval empty after the
    # floor (no evaluation possible due to data shortage — no LLM call was made) ·
    # 'degraded' = a guard/failure degraded the row. Additive with default: old envelopes
    # stay parseable, schema_version is unchanged.
    basis: str = 'llm'
    # The instrument's pair legs (ISSUE_70), attached by the engine from the SymbolSpec — never
    # scored by the LLM. `base_currency` is the asset side (e.g. ETH), `quote_currency` the quote
    # (e.g. USD). Additive with default: old envelopes stay parseable, schema_version unchanged.
    base_currency: Optional[str] = None
    quote_currency: Optional[str] = None


class SentimentLlmOutput(BaseModel):
    """The scored fields the LLM must return for one symbol (ISSUE_6).

    A strict subset of SentimentResult: the model scores the mood; provenance
    (`sources`), `is_breaking` and `symbol` are attached by the engine, never invented
    by the LLM. All fields required + no extras (`forbid`), so it maps cleanly to a
    JSON schema and rejects a malformed completion.
    """
    model_config = ConfigDict(extra='forbid')

    # Stays a `Literal`: this is the producing seam against the model, where a malformed
    # completion must fail rather than be absorbed.
    signal: SentimentSignal
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
    # Retrieval funnel per symbol (ISSUE_24): why a context was rich, thin or empty —
    # in-window candidates, floor drops, dedup collapses, kept. Additive, non-load-bearing.
    per_symbol_retrieval: Dict[str, RetrievalFunnel] = Field(default_factory=dict)
    # Variant grouping hints (ISSUE_42, additive — confirmed with the Testing IDE):
    # present only on streams of a fanned constellation. `variant_group` = the default
    # stream's pipeline_id ("this series derives from that one"); `variant` = this
    # stream's sub id. `pipeline_id == variant_group` ⇔ the default variant. A consumer
    # groups fan streams by these instead of parsing stream ids.
    variant_group: Optional[str] = None
    variant: Optional[str] = None

    @model_serializer(mode='wrap')
    def _omit_absent_hints(self, handler: SerializerFunctionWrapHandler) -> Dict[str, Any]:
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
    # 2.0 (ISSUE_9): a MAJOR bump, because `trigger_reason` moved out of `metadata` to the top
    # level — a Tier 3 relocation, i.e. a coordinated break, and the consumer's loader gates on
    # the major. Everything else in the group (seq, stream_epoch, available_msc,
    # evidence_as_of) is purely additive and would not have needed one.
    schema_version: str = '2.0'
    # Stream identity and position (ISSUE_9). Minted by the outcome store inside the envelope's own
    # insert transaction, so the series is gapless: a rolled-back pass returns its number and the
    # committed set is always a contiguous prefix. A gap therefore means exactly one thing to a
    # consumer — a record that never arrived. `stream_epoch` guards the one case `seq` cannot: a
    # restore rewinds the counter, the engine re-mints numbers the consumer already holds, and every
    # new frame would sit below their cursor and be ignored. The cursor is `(stream_epoch, seq)`,
    # and that pair is also a total chronological order — an epoch changes only at boot, i.e. at one
    # instant, so cross-era ordering needs no clock.
    # Optional because an envelope whose persistence failed genuinely has neither: `_persist`
    # degrades the pass and still serves the envelope. Absent = never persisted, never on the wire.
    seq: Optional[int] = None
    stream_epoch: Optional[int] = None
    pipeline_id: str
    outcome_type: str
    # Where the data came from. The engine always produces 'live'; the mock generator stamps
    # 'synthetic'. A naming convention alone could not carry this: the Testing IDE found a
    # generated week and a real week with byte-identical provenance, because the generator mirrors
    # `prompt_hash` on purpose (the prompt really is the same one) and only the date told them
    # apart — an unwritten rule no tool checks. The origin is a property of the data, so it travels
    # with the data. Default 'live' keeps pre-change archived envelopes parseable; a consumer reads
    # an absent field as "unknown, produced before this existed".
    data_origin: str = 'live'
    # Input provenance (ISSUE_85) — the configuration twin of `prompt_hash` below. Fingerprints
    # the *merged* pipeline config plus its *resolved* source set plus the score-defining slice
    # of the app config, so a feed added, disabled or re-weighted is visible downstream instead
    # of hiding behind byte-identical provenance (the archive's 2026-07-24 symbol expansion is
    # the live example). Two archive days are comparable when `prompt_hash` AND this agree.
    # Default '' keeps pre-change archived envelopes parseable; a consumer reads an absent field
    # as "unknown, produced before this existed" — never as "same as the neighbouring day".
    config_fingerprint: str = ''
    # Prompt provenance (ISSUE_33): `prompt_id` + `prompt_version` name the prompt series;
    # `prompt_hash` fingerprints the template body so a silent edit is visible downstream.
    # Populated from PromptMetadata when the envelope is assembled (ISSUE_7); default '' keeps
    # older archived envelopes (pre-ISSUE_33) parseable.
    prompt_version: str
    prompt_id: str = ''
    prompt_hash: str = ''
    # Why this pass ran (ISSUE_87) — resolved by the trigger, never guessed from the timestamp:
    #   'scheduled' the planned tick (bar close) · 'boot' the first pass after a process start ·
    #   'breaking'  an out-of-band wake (ISSUE_11) · 'manual' run_cli · 'external' POST /run.
    # A scheduled bar-close pass, a restart and a breaking wake were byte-indistinguishable
    # downstream before this (`is_breaking` is the LLM's confirmation, not the pass's cause), and
    # timing cannot substitute: `timestamp` is stamped at the END of a variable-length run, so ~6 %
    # of scheduled passes land off the cadence grid on their own.
    # Top-level rather than inside `metadata` (ISSUE_9): the contract declares `metadata.*` free to
    # evolve, and an exception carved into a free-to-evolve container is one the next person
    # violates while believing they are complying. Promoting it makes that rule absolute.
    # Always serialized: '' means "unknown, produced before this field existed" — a trigger reason
    # applies to every pass, so an absent value can only be old data, never "not applicable".
    # Plain `str`, not the `TriggerReason` Literal, so an archived envelope carrying a value a
    # later version introduced still parses (the envelope contract outranks type strictness).
    trigger_reason: str = ''
    timestamp: datetime
    # When the envelope became fetchable via /latest and pushable on the stream — the instant of the
    # outcome-store write, in epoch-ms UTC (ISSUE_9). The consumer's no-look-ahead gate: a snapshot
    # is visible to a decision at or after this, never at `timestamp` (analysis wall-clock, which is
    # informational) and never at the archiver's own receipt stamp, which differs per archiver.
    # A separate int field rather than promoting `timestamp`: the merge key is epoch-ms by contract
    # and `timestamp` is a datetime — one job with two types is exactly the parse drift the field
    # tiers exist to prevent.
    available_msc: Optional[int] = None
    # The monotonic clamp on that stamp. `available_msc` is the only value the engine samples from a
    # wall clock, so it is the only one that can step backwards (NTP, a manual change, a VM resume);
    # it is held at the previous value instead, and the correction is counted here rather than
    # merely survived. Named for the clock they describe — the cross-collector contract's `anchor_*`
    # stays reserved for a collector sampling its own `collected_msc`.
    available_msc_resyncs: Optional[int] = None
    available_msc_max_correction_ms: Optional[int] = None
    status: str
    result: List[T] = Field(default_factory=list)
    metadata: RunMetadata
    errors: List[RunError] = Field(default_factory=list)


# First concrete outcome type. Future: TrendEnvelope, CurrencyEventEnvelope, ...
SentimentEnvelope = AnalysisEnvelope[SentimentResult]
