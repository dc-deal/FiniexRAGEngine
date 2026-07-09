"""Pydantic config schema for the application — backs AppConfigManager.

Defaults mirror configs/app_config.json exactly (operator-visible, tunable).
"""
from typing import Dict

from pydantic import BaseModel, Field


class ApiConfig(BaseModel):
    host: str = '0.0.0.0'
    port: int = 8100


class LlmConfig(BaseModel):
    provider: str = 'openai'
    model: str = 'gpt-4o-mini'
    temperature: float = 0.1
    timeout_seconds: int = 30


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


class CostConfig(BaseModel):
    """Cost tracking knobs. Balance is not exposed by the API, so we derive it."""
    account_credit_usd: float = 0.0   # what you topped up; remaining ≈ credit − tracked spend
    budget_usd: float = 0.0           # optional soft cap for a spend warning (0 = off)


class AppConfig(BaseModel):
    version: str = '0.1.0'
    schema_version: str = '1.0'
    api: ApiConfig = Field(default_factory=ApiConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    log_level: str = 'INFO'
