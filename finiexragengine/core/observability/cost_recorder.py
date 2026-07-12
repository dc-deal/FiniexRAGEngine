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
    """Writes a cost_log row per paid call: tokens + derived USD + latency + section.

    One capture point for cost *and* performance (ISSUE_23/32): the caller passes the
    API call's `duration_ms` alongside the usage, so every row is also a latency sample —
    ts + section + model + pipeline_id + duration make a slow or hung call traceable.
    The recorder also accumulates a per-instance session total (tokens/USD), so any CLI
    can echo what *this* pass just spent without re-querying the log.
    """

    def __init__(self, database_url: str, pricing: PricingConfig,
                 table: str = 'cost_log') -> None:
        self._database_url = database_url
        self._pricing = pricing
        self._table = table
        # Session accumulators — what this process recorded (for the RunFooter echo).
        self._session_tokens = 0
        self._session_usd = 0.0
        self._ensure_schema()

    @property
    def session_tokens(self) -> int:
        return self._session_tokens

    @property
    def session_usd(self) -> float:
        return self._session_usd

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
                    'pipeline_id TEXT, '
                    'duration_ms DOUBLE PRECISION, '         # API-call latency (ISSUE_32)
                    'model_snapshot TEXT)')                  # served model (response.model)
                # In-place upgrades for tables created before these columns existed;
                # older rows keep NULL (real migrations: #14).
                cur.execute(f'ALTER TABLE {self._table} '
                            'ADD COLUMN IF NOT EXISTS duration_ms DOUBLE PRECISION')
                cur.execute(f'ALTER TABLE {self._table} '
                            'ADD COLUMN IF NOT EXISTS model_snapshot TEXT')
        except psycopg.Error as exc:
            raise VectorStoreError(f'cost-log schema init failed: {exc}') from exc

    def record(self, section: str, model: str, prompt_tokens: int,
               completion_tokens: int = 0, pipeline_id: Optional[str] = None,
               duration_ms: Optional[float] = None,
               model_snapshot: Optional[str] = None) -> float:
        """Write one cost_log row (tokens + USD + latency + served model); returns the USD.

        `model` is the configured name (the pricing-table key); `model_snapshot` is what
        the API actually served (`response.model`) — an alias retarget shows up here.
        """
        usd = derive_usd(self._pricing, model, prompt_tokens, completion_tokens)
        total = prompt_tokens + completion_tokens
        try:
            with self._connect() as conn, conn.cursor() as cur:
                # Alias-drift guard (#40): compare the served snapshot with the last one
                # recorded for this model — the alias is kept for convenience, but the
                # moment the provider retargets it, the signal series shifts and the
                # operator must know. (Yellow/rich rendering rides #25.)
                if model_snapshot:
                    cur.execute(
                        f'SELECT model_snapshot FROM {self._table} '
                        'WHERE model = %s AND model_snapshot IS NOT NULL '
                        'ORDER BY id DESC LIMIT 1', (model,))
                    last = cur.fetchone()
                    if last and last[0] != model_snapshot:
                        logger.warning(
                            "model alias '%s' was retargeted: now serving '%s' "
                            "(previously '%s') — the signal series shifts here",
                            model, model_snapshot, last[0])
                cur.execute(
                    f'INSERT INTO {self._table} (section, model, prompt_tokens, '
                    'completion_tokens, total_tokens, usd_cost, pipeline_id, duration_ms, '
                    'model_snapshot) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    (section, model, prompt_tokens, completion_tokens, total, usd,
                     pipeline_id, duration_ms, model_snapshot or None))
        except psycopg.Error as exc:
            raise VectorStoreError(f'cost-log write failed: {exc}') from exc
        self._session_tokens += total
        self._session_usd += usd
        return usd
