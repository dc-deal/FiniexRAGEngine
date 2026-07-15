"""Pydantic config schema for the application — backs AppConfigManager.

Defaults mirror configs/app_config.json exactly (operator-visible, tunable).
"""
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ApiConfig(BaseModel):
    host: str = '0.0.0.0'
    port: int = 8100


class LlmConfig(BaseModel):
    """Call mechanics + model governance — deliberately WITHOUT a global model.

    The eval model is series-defining (like the prompt): each pipeline declares its own
    (`pipeline.llm.model`), so a global edit can never silently shift every signal
    series at once. This block only governs *how* calls are made and *which* models are
    admissible at all.
    """
    provider: str = 'openai'
    temperature: float = 0.1
    timeout_seconds: int = 30
    # Governance allowlist: a pipeline requesting a model outside this set fails at
    # assembly — fail fast, before any spend. Override the list in the gitignored
    # user_configs to admit e.g. a fine-tuned `ft:...` model without touching tracked config.
    allowed_models: List[str] = Field(
        default_factory=lambda: ['gpt-4o-mini', 'gpt-4o'])
    # Optional OpenAI-compatible endpoint (vLLM, Ollama, ...) for self-hosted models —
    # private infrastructure, so it belongs in the user_configs override.
    base_url: Optional[str] = None


class EmbeddingConfig(BaseModel):
    provider: str = 'openai'
    model: str = 'text-embedding-3-small'
    dimensions: int = 1536


class VectorStoreConfig(BaseModel):
    backend: str = 'pgvector'
    table: str = 'articles'
    retrieval_top_k: int = 12
    recency_window_minutes: int = 1440


class ModelPrice(BaseModel):
    """USD price per 1K tokens for one model (embeddings have output_per_1k = 0)."""
    input_per_1k: float = 0.0
    output_per_1k: float = 0.0


# Published OpenAI rates per 1K tokens — there is no pricing API, so this is a
# hand-maintained table (update it when OpenAI changes prices). Mirrors
# configs/app_config.json `pricing.models`.
_DEFAULT_MODEL_PRICES = {
    'text-embedding-3-small': ModelPrice(input_per_1k=0.00002),
    'text-embedding-3-large': ModelPrice(input_per_1k=0.00013),
    'gpt-4o-mini': ModelPrice(input_per_1k=0.00015, output_per_1k=0.0006),
    'gpt-4o': ModelPrice(input_per_1k=0.0025, output_per_1k=0.01),
}


class PricingConfig(BaseModel):
    """Per-model token prices — the reproducible basis for deriving USD from usage."""
    currency: str = 'USD'
    models: Dict[str, ModelPrice] = Field(
        default_factory=lambda: dict(_DEFAULT_MODEL_PRICES))


class CircuitBreakerConfig(BaseModel):
    """Cost circuit-breaker (ISSUE_47) — react to the provider's own spend limit.

    The hard stop is the provider itself: OpenAI returns HTTP 429 `insufficient_quota` at the
    account ceiling. This block only governs how we *react* — on that signal, suspend paid work,
    back off, and re-probe once per cool-off (auto-resume). `soft_daily_usd` is an optional
    warn-only early line *under* that ceiling; it never suspends (the provider stays the hard stop).
    """
    enabled: bool = True                   # master switch for the reaction
    reprobe_interval_seconds: int = 600    # cool-off before one re-probe after a quota suspend
    soft_daily_usd: float = 0.0            # warn-only day line (0 = off); does NOT suspend


class CostConfig(BaseModel):
    """Cost tracking knobs. Balance is not exposed by the API, so we derive it."""
    account_credit_usd: float = 0.0   # what you topped up; remaining ≈ credit − tracked spend
    budget_usd: float = 0.0           # optional soft cap for a spend warning (0 = off)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)   # ISSUE_47


class LoggingConfig(BaseModel):
    """File logging + rotation (ISSUE_11). The console handler stays on regardless — this
    only adds a flat, rotating file so an overnight worker run survives the scrollback and
    stays grep-able the morning after. The *level* is `AppConfig.log_level` (shared with the
    console); this block is purely the file + noise policy.
    """
    file: Optional[str] = 'logs/finiex.log'    # rotating log path; set null for console-only
    rotation: Literal['daily', 'size'] = 'daily'
    backup_count: int = 14                     # rotated files kept (daily: days; size: files)
    max_bytes: int = 10_000_000                # size-rotation only (ignored when rotation='daily')
    # Third-party loggers pinned to WARNING so the file is signal, not per-request noise
    # (httpx logs every OpenAI call at INFO — thousands a night otherwise).
    quiet_loggers: List[str] = Field(default_factory=lambda: ['httpx', 'httpcore'])


class SourceHealthConfig(BaseModel):
    """Source-health flagging policy (ISSUE_11) — app-wide, not per source-set.

    A feed that keeps failing (rate-limit, malformed body, TLS drop) is flagged and
    quarantined: polling pauses for `quarantine_hours`, then it is retried once; still
    failing → flagged again. The last few warnings/errors are kept per source so the
    Sources report / weekly is debugging-ready without digging through logs.
    """
    flag_after_consecutive_failures: int = 5   # consecutive fails -> flag + quarantine
    quarantine_hours: int = 24                  # a flagged source is skipped this long, then retried
    recent_events_kept: int = 10                # capped warn/error ring per source (overview)


class AppConfig(BaseModel):
    version: str = '0.2.0'
    schema_version: str = '1.0'
    api: ApiConfig = Field(default_factory=ApiConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    log_level: str = 'INFO'
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    source_health: SourceHealthConfig = Field(default_factory=SourceHealthConfig)
