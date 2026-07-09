"""OpenAI-backed embedder (text-embedding-3-small by default)."""
from typing import TYPE_CHECKING, List, Optional

from openai import OpenAI, OpenAIError

from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.exceptions.ragengine_errors import EmbeddingError
from finiexragengine.types.config_types.app_config_types import EmbeddingConfig

if TYPE_CHECKING:
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
                 section: str = 'embed', pipeline_id: Optional[str] = None) -> None:
        self._config = config
        self._client = client   # built lazily from OPENAI_API_KEY if not injected
        # Optional cost capture (ISSUE_23): if a recorder is set, each embed() call
        # logs its token usage under `section` (e.g. 'ingest_news' | 'ingest_query').
        self._cost_recorder = cost_recorder
        self._section = section
        self._pipeline_id = pipeline_id

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI()   # reads OPENAI_API_KEY from the environment
        return self._client

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        client = self._get_client()
        vectors: List[List[float]] = []
        prompt_tokens = 0
        for start in range(0, len(texts), self._MAX_BATCH):
            batch = texts[start:start + self._MAX_BATCH]
            try:
                response = client.embeddings.create(
                    model=self._config.model,
                    input=batch,
                    dimensions=self._config.dimensions,
                )
            except OpenAIError as exc:
                raise EmbeddingError(f'embedding request failed: {exc}') from exc
            # Accumulate the paid token usage across batches (irreconstructable later).
            usage = getattr(response, 'usage', None)
            if usage is not None:
                prompt_tokens += getattr(usage, 'prompt_tokens', 0) or 0
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
        # Record the spend once per embed() call — cost is never silent (ISSUE_23).
        if self._cost_recorder is not None and prompt_tokens:
            self._cost_recorder.record(self._section, self._config.model,
                                       prompt_tokens, 0, self._pipeline_id)
        return vectors
