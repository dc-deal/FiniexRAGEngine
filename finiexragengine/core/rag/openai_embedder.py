"""OpenAI-backed embedder (text-embedding-3-small by default)."""
from time import perf_counter
from typing import TYPE_CHECKING, List, Optional

from openai import OpenAI, OpenAIError

from finiexragengine.core.llm.openai_quota import is_quota_exceeded
from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.exceptions.ragengine_errors import BudgetExceededError, EmbeddingError
from finiexragengine.types.config_types.app_config_types import EmbeddingConfig

if TYPE_CHECKING:
    from finiexragengine.core.observability.budget_guard import BudgetGuard
    from finiexragengine.core.observability.cost_recorder import CostRecorder


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

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI()   # reads OPENAI_API_KEY from the environment
        return self._client

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        # Circuit-breaker gate (ISSUE_47): refuse before the call while paid work is suspended,
        # so the ingest pass / eval degrades cleanly instead of a doomed request.
        if self._budget_guard is not None and not self._budget_guard.should_attempt():
            raise BudgetExceededError('embedding suspended — provider quota reached')
        client = self._get_client()
        vectors: List[List[float]] = []
        prompt_tokens = 0
        api_ms = 0.0
        served_model = ''
        for start in range(0, len(texts), self._MAX_BATCH):
            batch = texts[start:start + self._MAX_BATCH]
            call_start = perf_counter()
            try:
                response = client.embeddings.create(
                    model=self._config.model,
                    input=batch,
                    dimensions=self._config.dimensions,
                )
            except OpenAIError as exc:
                # A quota exhaustion is a budget stop → arm the breaker + BUDGET_EXCEEDED
                # (ISSUE_47); anything else stays the embedding-error path.
                if self._budget_guard is not None and is_quota_exceeded(exc):
                    self._budget_guard.on_quota_error(reason=getattr(exc, 'code', None) or 'quota')
                    raise BudgetExceededError(
                        f'embedding suspended — provider quota reached: {exc}') from exc
                raise EmbeddingError(f'embedding request failed: {exc}') from exc
            # Sum pure API time across batches — the latency sample next to the tokens (ISSUE_32).
            api_ms += (perf_counter() - call_start) * 1000.0
            # Accumulate the paid token usage across batches (irreconstructable later).
            usage = getattr(response, 'usage', None)
            if usage is not None:
                prompt_tokens += getattr(usage, 'prompt_tokens', 0) or 0
            # Served model (response.model). Embedding ids carry no alias/snapshot pair —
            # the id IS the version (vectors across models are incompatible, so OpenAI
            # ships changes as new ids). Captured anyway: if the id were ever silently
            # retargeted, the corpus would mix vector spaces — the alias-drift guard in
            # CostRecorder then fires for ingest rows too (ISSUE_40).
            served_model = getattr(response, 'model', '') or served_model
            # OpenAI returns L2-normalized (unit-length) vectors, so downstream a
            # dot product already equals cosine similarity and pgvector's <=>
            # distance needs no separate normalization step.
            # The API may return items unordered; `.index` is the position in `batch`.
            ordered = sorted(response.data, key=lambda item: item.index)
            for item in ordered:
                if len(item.embedding) != self._config.dimensions:
                    raise EmbeddingError(
                        f'expected dimension {self._config.dimensions}, '
                        f'got {len(item.embedding)}')
                vectors.append(list(item.embedding))
        # Record the spend once per embed() call — cost is never silent (ISSUE_23);
        # the API duration rides the same row as the latency sample (ISSUE_32).
        recorded_usd = 0.0
        if self._cost_recorder is not None and prompt_tokens:
            recorded_usd = self._cost_recorder.record(self._section, self._config.model,
                                       prompt_tokens, 0, self._pipeline_id,
                                       duration_ms=api_ms,
                                       model_snapshot=served_model or None)
        # A successful call proves quota is available → clear any suspend + feed the day warn (ISSUE_47).
        if self._budget_guard is not None:
            self._budget_guard.record_spend(recorded_usd)
        return vectors
