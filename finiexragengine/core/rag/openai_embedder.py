"""OpenAI-backed embedder (text-embedding-3-small by default)."""
import logging
from time import perf_counter
from typing import TYPE_CHECKING, List, Optional, Tuple

from openai import BadRequestError, OpenAI, OpenAIError

from finiexragengine.core.llm.openai_quota import is_quota_exceeded
from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.core.rag.token_budget import FittedText, TokenBudget
from finiexragengine.exceptions.ragengine_errors import BudgetExceededError, EmbeddingError
from finiexragengine.types.config_types.app_config_types import EmbeddingConfig
from finiexragengine.types.embedding_types import EmbedResult

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
        # Optional cost capture (ISSUE_23): if a recorder is set, each embed() call
        # logs its token usage under `section` (e.g. 'ingest_news' | 'ingest_query').
        self._cost_recorder = cost_recorder
        self._section = section
        self._pipeline_id = pipeline_id
        # Cost circuit-breaker (ISSUE_47): gates the paid embed and reacts to the quota signal —
        # guards ingest *and* query embedding (a suspended query embed degrades the eval too).
        self._budget_guard = budget_guard
        # The model's input limit (ISSUE_79). Resolved here so an unknown model fails at
        # construction — at boot, where the startup check surfaces it — not mid-pass.
        self._budget = TokenBudget(config)

    def _get_client(self) -> OpenAI:
        if self._client is None:
            # An explicit deadline (ISSUE_79): without one the SDK default applies (600s read x 2
            # retries ~= 30 min of a dead worker), which made this the last un-timeouted network
            # call in the ingest path and the likeliest cause of passes blowing the #74 deadline.
            self._client = OpenAI(timeout=self._config.timeout_seconds)
        return self._client

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
        api_ms = 0.0
        served_model = ''
        # 2. Send in batches; a batch the provider rejects wholesale is bisected rather than lost.
        for start in range(0, len(texts), self._MAX_BATCH):
            indices = list(range(start, min(start + self._MAX_BATCH, len(texts))))
            batch_ms, model = self._embed_indices(client, indices, fitted, result)
            api_ms += batch_ms
            served_model = model or served_model
        # 3. Does the provider count what we counted? Our sum is tiktoken's, `billed_tokens` is
        #    theirs. A divergence means the encoding assumption has drifted (a retargeted model id,
        #    a stale tiktoken table) — the same class of silent shift the alias-drift guard watches
        #    for. A canary, never a gate: it warns and the pass continues.
        if result.billed_tokens and result.billed_tokens != result.counted_tokens:
            logger.warning('[EMBED] token count mismatch: counted %d locally, provider billed %d '
                           '(model %s) — the tokenizer assumption may have drifted',
                           result.counted_tokens, result.billed_tokens, self._config.model)
        # Record the spend once per embed() call — cost is never silent (ISSUE_23);
        # the API duration rides the same row as the latency sample (ISSUE_32).
        recorded_usd = 0.0
        if self._cost_recorder is not None and result.billed_tokens:
            recorded_usd = self._cost_recorder.record(self._section, self._config.model,
                                       result.billed_tokens, 0, self._pipeline_id,
                                       duration_ms=api_ms,
                                       model_snapshot=served_model or None)
        # A successful call proves quota is available → clear any suspend + feed the day warn (ISSUE_47).
        if self._budget_guard is not None:
            self._budget_guard.record_spend(recorded_usd)
        return result

    def _embed_indices(self, client: OpenAI, indices: List[int], fitted: List[FittedText],
                       result: EmbedResult) -> Tuple[float, str]:
        """Embed the inputs at `indices`, filling `result` in place; returns (api_ms, served_model).

        Bisects on a provider rejection (ISSUE_79). A `BadRequestError` means *some* item in this
        batch is unacceptable — the provider names an index but not reliably enough to trust
        across SDK versions, so we halve and retry until the offender is alone and can be recorded
        as rejected while everything else still embeds. Bounded: ~2·log2(n) extra calls in the
        failure case, none in the normal one.

        The trigger is deliberately narrow. Quota errors keep raising `BudgetExceededError`
        (ISSUE_47) and transient failures stay with the SDK's own retry — bisecting those would
        multiply a temporary outage instead of isolating a permanent defect.
        """
        if not indices:
            return 0.0, ''
        call_start = perf_counter()
        try:
            response = client.embeddings.create(
                model=self._config.model,
                input=[fitted[i].text for i in indices],
                dimensions=self._config.dimensions,
            )
        except BadRequestError as exc:
            api_ms = (perf_counter() - call_start) * 1000.0
            if len(indices) == 1:
                # Isolated: this single input is the one the provider will not take.
                result.rejected.append(indices[0])
                result.input_tokens[indices[0]] = None
                logger.warning('[EMBED] input rejected by the provider and skipped '
                               '(%d tokens after fitting): %s',
                               fitted[indices[0]].tokens, exc)
                return api_ms, ''
            middle = len(indices) // 2
            left_ms, left_model = self._embed_indices(client, indices[:middle], fitted, result)
            right_ms, right_model = self._embed_indices(client, indices[middle:], fitted, result)
            return api_ms + left_ms + right_ms, left_model or right_model
        except OpenAIError as exc:
            # A quota exhaustion is a budget stop → arm the breaker + BUDGET_EXCEEDED
            # (ISSUE_47); anything else stays the embedding-error path.
            if self._budget_guard is not None and is_quota_exceeded(exc):
                self._budget_guard.on_quota_error(reason=getattr(exc, 'code', None) or 'quota')
                raise BudgetExceededError(
                    f'embedding suspended — provider quota reached: {exc}') from exc
            raise EmbeddingError(f'embedding request failed: {exc}') from exc
        api_ms = (perf_counter() - call_start) * 1000.0
        # Accumulate the paid token usage across batches (irreconstructable later).
        usage = getattr(response, 'usage', None)
        if usage is not None:
            result.billed_tokens += getattr(usage, 'prompt_tokens', 0) or 0
        # Served model (response.model). Embedding ids carry no alias/snapshot pair —
        # the id IS the version (vectors across models are incompatible, so OpenAI
        # ships changes as new ids). Captured anyway: if the id were ever silently
        # retargeted, the corpus would mix vector spaces — the alias-drift guard in
        # CostRecorder then fires for ingest rows too (ISSUE_40).
        served_model = getattr(response, 'model', '') or ''
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
        return api_ms, served_model
