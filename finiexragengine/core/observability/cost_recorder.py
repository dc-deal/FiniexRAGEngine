"""Cost recorder — one billing-log row per paid API call (ISSUE_23).

USD is derived from the configured price table **at record time** and stored on the row,
so a later price change never rewrites history: the token counts are the ground truth
(exact, from the API `usage`), the USD is a frozen derivation. An unknown model logs a
warning and costs 0.0 — a new, unpriced model is visible, not silently free.
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.config_types.app_config_types import PricingConfig

logger = logging.getLogger(__name__)


@dataclass
class PassSpend:
    """What one pass spent — the accumulator a `pass_scope()` hands out (ISSUE_74)."""
    tokens: int = 0
    usd: float = 0.0

    def add(self, tokens: int, usd: float) -> None:
        self.tokens += tokens
        self.usd += usd


# The active pass's accumulator. A ContextVar, not a thread-local, because the pass body runs via
# `asyncio.to_thread`, which copies the current context at call time and runs the body under
# `ctx.run` — so a scope opened on the event loop reaches the worker thread, and two concurrent
# passes each get their own binding (verified, not assumed). The accumulator is mutable and shared
# by reference, so the value written inside the thread is readable back on the loop afterwards.
_CURRENT_PASS: ContextVar[Optional[PassSpend]] = ContextVar('finiex_pass_spend', default=None)

# Why the active pass runs (ISSUE_87). A second ContextVar rather than a field on `PassSpend`,
# because the two answer different questions and have different lifetimes: the accumulator is
# written back by every call, the reason is read-only for the whole pass. Ambient by design —
# threading it through the embedder and the provider would mean touching every paid call site to
# carry a value none of them uses. '' outside a scope (a CLI recording without one).
_CURRENT_REASON: ContextVar[str] = ContextVar('finiex_trigger_reason', default='')


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

    @property
    def session_tokens(self) -> int:
        return self._session_tokens

    @property
    def session_usd(self) -> float:
        return self._session_usd

    @contextmanager
    def pass_scope(self, reason: str = '') -> Iterator[PassSpend]:
        """Account one pass's spend in isolation (ISSUE_74), under why it runs (ISSUE_87).

        Replaces the session-delta idiom (`usd_before = recorder.session_usd` … subtract after),
        which only produced the right number while every pass was serialized by the workers'
        shared lock. That lock is what turned one hung feed into a nine-day engine outage on
        2026-08-01, so the attribution had to stop depending on it: a scope accumulates only the
        calls made *within it*, and concurrent passes cannot cross-attribute by construction.

        The session totals keep accumulating alongside — `ingest_cli` reads them for its footer.

        `reason` (ISSUE_87) is stamped onto every cost row the pass produces, so "what do
        out-of-band wakes cost us" is a GROUP BY instead of a reconstruction. Default '' keeps a
        scope opened for accounting alone (tests, a CLI) honest about not knowing.
        """
        spend = PassSpend()
        token = _CURRENT_PASS.set(spend)
        reason_token = _CURRENT_REASON.set(reason)
        try:
            yield spend
        finally:
            _CURRENT_PASS.reset(token)
            _CURRENT_REASON.reset(reason_token)

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the cost log: {exc}') from exc

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
                    'model_snapshot, trigger_reason) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    (section, model, prompt_tokens, completion_tokens, total, usd,
                     pipeline_id, duration_ms, model_snapshot or None,
                     # Why the enclosing pass runs (ISSUE_87) — read off the scope, so no paid
                     # call site has to carry a value it never uses. NULL outside a scope.
                     _CURRENT_REASON.get() or None))
        except psycopg.Error as exc:
            raise VectorStoreError(f'cost-log write failed: {exc}') from exc
        self._session_tokens += total
        self._session_usd += usd
        # Attribute to the enclosing pass, if any (ISSUE_74). None outside a scope — the CLI
        # paths record without one and read the session totals instead.
        current = _CURRENT_PASS.get()
        if current is not None:
            current.add(total, usd)
        return usd
