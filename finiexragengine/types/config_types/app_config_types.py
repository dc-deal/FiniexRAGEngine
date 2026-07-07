"""Pydantic config schema for the application — backs AppConfigManager.

Defaults mirror configs/app_config.json exactly (operator-visible, tunable).
"""
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


class AppConfig(BaseModel):
    version: str = '0.1.0'
    schema_version: str = '1.0'
    api: ApiConfig = Field(default_factory=ApiConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    log_level: str = 'INFO'
