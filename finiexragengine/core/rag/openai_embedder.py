"""OpenAI-backed embedder (text-embedding-3-small by default)."""
import logging
from typing import TYPE_CHECKING, List, Optional

from openai import BadRequestError, OpenAI, OpenAIError

from finiexragengine.core.llm.openai_call_runner import OpenAICallRunner
from finiexragengine.core.llm.openai_quota import is_quota_exceeded
from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.core.rag.token_budget import FittedText, TokenBudget
from finiexragengine.exceptions.ragengine_errors import BudgetExceededError, EmbeddingError
from finiexragengine.types.config_types.app_config_types import EmbeddingConfig
from finiexragengine.types.embedding_types import EmbedResult
from finiexragengine.types.llm_types import AttemptOutcome

if TYPE_CHECKING:
    from finiexragengine.core.observability.budget_guard import BudgetGuard
    from finiexragengine.core.observability.cost_recorder import CostRecorder

logger = logging.getLogger(__name__)


class OpenAIEmbedder(AbstractEmbedder):
    """Embeds via the OpenAI embeddings API.

    Used for both ingest (articles) and query (per-symbol retrieval). The output
    width is pinned to `config.dimensions` — the pgvector column width — via the
    API's `dimensions` parameter, so a config change can never desync the store.
    A local sentence-transformers embedder is the drop-in alternative (same
    AbstractEmbedder contract).
    """

    _MAX_BATCH = 256   # inputs per request; large corpora are chunked, order preserved

    def __init__(self, config: EmbeddingConfig, client: Optional[OpenAI] = None,
                 cost_recorder: Optional['CostRecorder'] = None,
                 section: str = 'embed', pipeline_id: Optional[str] = None,
                 budget_guard: Optional['BudgetGuard'] = None) -> None:
        self._config = config
        self._client = client   # built lazily from OPENAI_API_KEY if not injected
        # Optional cost capture (ISSUE_23): if a recorder is set, every batch request logs its
        # token usage under `section` (e.g. 'ingest_news' | 'ingest_query') — one row per attempt
        # (ISSUE_128), so a batch that succeeded before a later one failed is never lost.
        self._cost_recorder = cost_recorder
        self._section = section
        self._pipeline_id = pipeline_id
        # Cost circuit-breaker (ISSUE_47): gates the paid embed and reacts to the quota signal —
        # guards ingest *and* query embedding (a suspended query embed degrades the eval too).
        self._budget_guard = budget_guard
        # The model's input limit (ISSUE_79). Resolved here so an unknown model fails at
        # construction — at boot, where the startup check surfaces it — not mid-pass.
        self._budget = TokenBudget(config)
        # Our retry loop, not the SDK's (ISSUE_128): every attempt visible and recorded.
        self._runner = OpenAICallRunner(f'[EMBED] {pipeline_id or section} · {config.model}',
                                        config.max_retries, float(config.timeout_seconds),
                                        on_failed_attempt=self._record_failed_attempt)

    def _get_client(self) -> OpenAI:
        if self._client is None:
            # An explicit deadline (ISSUE_79): without one the SDK default applies (600s read x 2
            # retries ~= 30 min of a dead worker), which made this the last un-timeouted network
            # call in the ingest path and the likeliest cause of passes blowing the #74 deadline.
            # max_retries=0: the runner retries, so no attempt happens out of sight (ISSUE_128).
            self._client = OpenAI(timeout=self._config.timeout_seconds, max_retries=0)
        return self._client

    def _record_failed_attempt(self, attempt: int, outcome: AttemptOutcome,
                               duration_ms: float) -> None:
        """One row for a batch attempt that did not answer — spend unknown, so NULL."""
        if self._cost_recorder is None:
            return
        self._cost_recorder.record(self._section, self._config.model, None, None,
                                   self._pipeline_id, duration_ms=duration_ms,
                                   attempt=attempt, outcome=outcome)

    def embed(self, texts: List[str]) -> EmbedResult:
        if not texts:
            return EmbedResult()
        # Circuit-breaker gate (ISSUE_47): refuse before the call while paid work is suspended,
        # so the ingest pass / eval degrades cleanly instead of a doomed request.
        if self._budget_guard is not None and not self._budget_guard.should_attempt():
            raise BudgetExceededError('embedding suspended — provider quota reached')
        client = self._get_client()
        # 1. Fit every input to the model's limit before anything is sent (ISSUE_79). Both counts
        #    fall out of the encode the limit check does anyway.
        fitted = [self._budget.fit(text) for text in texts]
        result = EmbedResult(
            vectors=[None] * len(texts),
            input_tokens=[item.tokens for item in fitted],
            truncated_tokens=[item.dropped_tokens for item in fitted],
        )
        # 2. Send in batches; a batch the provider rejects wholesale is bisected rather than lost.
        #    Each batch request records its own row as it answers (ISSUE_128).
        for start in range(0, len(texts), self._MAX_BATCH):
            indices = list(range(start, min(start + self._MAX_BATCH, len(texts))))
            self._embed_indices(client, indices, fitted, result)
        # 3. Does the provider count what we counted? Our sum is tiktoken's, `billed_tokens` is
        #    theirs. A divergence means the encoding assumption has drifted (a retargeted model id,
        #    a stale tiktoken table) — the same class of silent shift the alias-drift guard watches
        #    for. A canary, never a gate: it warns and the pass continues.
        if result.billed_tokens and result.billed_tokens != result.counted_tokens:
            logger.warning('[EMBED] token count mismatch: counted %d locally, provider billed %d '
                           '(model %s) — the tokenizer assumption may have drifted',
                           result.counted_tokens, result.billed_tokens, self._config.model)
        return result

    def _embed_indices(self, client: OpenAI, indices: List[int], fitted: List[FittedText],
                       result: EmbedResult) -> None:
        """Embed the inputs at `indices`, filling `result` in place and recording each request.

        Bisects on a provider rejection (ISSUE_79). A `BadRequestError` means *some* item in this
        batch is unacceptable — the provider names an index but not reliably enough to trust
        across SDK versions, so we halve and retry until the offender is alone and can be recorded
        as rejected while everything else still embeds. Bounded: ~2·log2(n) extra calls in the
        failure case, none in the normal one.

        The trigger is deliberately narrow. Quota errors keep raising `BudgetExceededError`
        (ISSUE_47) and transient failures stay with the call runner's retry (ISSUE_128) —
        bisecting those would multiply a temporary outage instead of isolating a permanent defect.
        """
        if not indices:
            return
        inputs = [fitted[i].text for i in indices]
        try:
            answered = self._runner.run(lambda timeout: client.embeddings.create(
                model=self._config.model,
                input=inputs,
                dimensions=self._config.dimensions,
                timeout=timeout,
            ))
        except BadRequestError as exc:
            if len(indices) == 1:
                # Isolated: this single input is the one the provider will not take.
                result.rejected.append(indices[0])
                result.input_tokens[indices[0]] = None
                logger.warning('[EMBED] input rejected by the provider and skipped '
                               '(%d tokens after fitting): %s',
                               fitted[indices[0]].tokens, exc)
                return
            middle = len(indices) // 2
            self._embed_indices(client, indices[:middle], fitted, result)
            self._embed_indices(client, indices[middle:], fitted, result)
            return
        except OpenAIError as exc:
            # A quota exhaustion is a budget stop → arm the breaker + BUDGET_EXCEEDED
            # (ISSUE_47); anything else stays the embedding-error path.
            if self._budget_guard is not None and is_quota_exceeded(exc):
                self._budget_guard.on_quota_error(reason=getattr(exc, 'code', None) or 'quota')
                raise BudgetExceededError(
                    f'embedding suspended — provider quota reached: {exc}') from exc
            raise EmbeddingError(f'embedding request failed: {exc}') from exc
        response = answered.value
        # The paid token usage of this request (irreconstructable later), accumulated across
        # batches for the tokenizer canary in `embed()`.
        usage = getattr(response, 'usage', None)
        billed = (getattr(usage, 'prompt_tokens', 0) or 0) if usage is not None else 0
        result.billed_tokens += billed
        # Served model (response.model). Embedding ids carry no alias/snapshot pair —
        # the id IS the version (vectors across models are incompatible, so OpenAI
        # ships changes as new ids). Captured anyway: if the id were ever silently
        # retargeted, the corpus would mix vector spaces — the alias-drift guard in
        # CostRecorder then fires for ingest rows too (ISSUE_40).
        served_model = getattr(response, 'model', '') or ''
        # Record this request now — cost is never silent (ISSUE_23), the answering attempt's
        # duration is its latency sample (ISSUE_32), and a later batch failing cannot take this
        # row with it (ISSUE_128).
        recorded_usd = 0.0
        if self._cost_recorder is not None and billed:
            recorded_usd = self._cost_recorder.record(self._section, self._config.model, billed, 0,
                                                      self._pipeline_id,
                                                      duration_ms=answered.duration_ms,
                                                      model_snapshot=served_model or None,
                                                      attempt=answered.attempt, outcome='ok')
        # A successful request proves quota is available → clear any suspend + feed the day warn
        # (ISSUE_47).
        if self._budget_guard is not None:
            self._budget_guard.record_spend(recorded_usd)
        # OpenAI returns L2-normalized (unit-length) vectors, so downstream a
        # dot product already equals cosine similarity and pgvector's <=>
        # distance needs no separate normalization step.
        # The API may return items unordered; `.index` is the position in this batch.
        for item in sorted(response.data, key=lambda entry: entry.index):
            if len(item.embedding) != self._config.dimensions:
                raise EmbeddingError(
                    f'expected dimension {self._config.dimensions}, '
                    f'got {len(item.embedding)}')
            result.vectors[indices[item.index]] = list(item.embedding)
