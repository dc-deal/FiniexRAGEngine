"""Cost recorder — one billing-log row per paid API call (ISSUE_23).

USD is derived from the configured price table **at record time** and stored on the row,
so a later price change never rewrites history: the token counts are the ground truth
(exact, from the API `usage`), the USD is a frozen derivation. An unknown model logs a
warning and costs 0.0 — a new, unpriced model is visible, not silently free.
"""
import logging
from typing import Optional

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.config_types.app_config_types import PricingConfig

logger = logging.getLogger(__name__)


def derive_usd(pricing: PricingConfig, model: str, prompt_tokens: int,
               completion_tokens: int = 0) -> float:
    """USD from the price table; unknown model -> warn + 0.0 (embeddings: output = 0)."""
    model_price = pricing.models.get(model)
    if model_price is None:
        logger.warning('no price for model %r — cost recorded as 0.0 (add it to '
                       'app_config.json pricing.models)', model)
        return 0.0
    return (prompt_tokens / 1000.0 * model_price.input_per_1k
            + completion_tokens / 1000.0 * model_price.output_per_1k)


class CostRecorder:
    """Writes a cost_log row per paid call: tokens + derived USD + section."""

    def __init__(self, database_url: str, pricing: PricingConfig,
                 table: str = 'cost_log') -> None:
        self._database_url = database_url
        self._pricing = pricing
        self._table = table
        self._ensure_schema()

    def _connect(self):
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the cost log: {exc}') from exc

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'CREATE TABLE IF NOT EXISTS {self._table} ('
                    'id BIGSERIAL PRIMARY KEY, '
                    'ts TIMESTAMPTZ NOT NULL DEFAULT now(), '
                    'section TEXT NOT NULL, '           # ingest_news | ingest_query | llm_eval | …
                    'model TEXT NOT NULL, '
                    'prompt_tokens INTEGER NOT NULL, '
                    'completion_tokens INTEGER NOT NULL DEFAULT 0, '
                    'total_tokens INTEGER NOT NULL, '
                    'usd_cost DOUBLE PRECISION NOT NULL, '   # frozen at record time
                    'pipeline_id TEXT)')
        except psycopg.Error as exc:
            raise VectorStoreError(f'cost-log schema init failed: {exc}') from exc

    def record(self, section: str, model: str, prompt_tokens: int,
               completion_tokens: int = 0, pipeline_id: Optional[str] = None) -> float:
        """Write one cost_log row; returns the derived USD cost."""
        usd = derive_usd(self._pricing, model, prompt_tokens, completion_tokens)
        total = prompt_tokens + completion_tokens
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'INSERT INTO {self._table} (section, model, prompt_tokens, '
                    'completion_tokens, total_tokens, usd_cost, pipeline_id) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                    (section, model, prompt_tokens, completion_tokens, total, usd, pipeline_id))
        except psycopg.Error as exc:
            raise VectorStoreError(f'cost-log write failed: {exc}') from exc
        return usd
