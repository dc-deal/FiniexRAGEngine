"""Unit tests for OpenAIEmbedder — order preservation, batching, dimension, failure.

The OpenAI client is faked, so these run offline with no API key. The fake returns
each batch's items in scrambled order (with correct `.index`) to prove the embedder
re-aligns to the input order.
"""
import random

import pytest

pytest.importorskip('openai')
from openai import OpenAIError  # noqa: E402

from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder  # noqa: E402
from finiexragengine.exceptions.ragengine_errors import EmbeddingError  # noqa: E402
from finiexragengine.types.config_types.app_config_types import EmbeddingConfig  # noqa: E402

_DIMS = 4


class _Item:
    def __init__(self, index: int, embedding: list) -> None:
        self.index = index
        self.embedding = embedding


class _Response:
    def __init__(self, data: list) -> None:
        self.data = data


class _Embeddings:
    """Encodes each input `text-N` as the vector [N]*dims, then scrambles the batch."""

    def __init__(self, parent: '_FakeClient', bad_dim: bool, boom: bool) -> None:
        self._parent = parent
        self._bad_dim = bad_dim
        self._boom = boom

    def create(self, model: str, input: list, dimensions: int) -> _Response:
        self._parent.calls.append({'model': model, 'n': len(input), 'dimensions': dimensions})
        if self._boom:
            raise OpenAIError('simulated API failure')
        width = dimensions + 1 if self._bad_dim else dimensions
        data = [_Item(i, [float(int(text.split('-')[1]))] * width)
                for i, text in enumerate(input)]
        random.Random(0).shuffle(data)
        return _Response(data)


class _FakeClient:
    def __init__(self, bad_dim: bool = False, boom: bool = False) -> None:
        self.calls: list = []
        self.embeddings = _Embeddings(self, bad_dim, boom)


def _embedder(**kwargs) -> OpenAIEmbedder:
    config = EmbeddingConfig(dimensions=_DIMS)
    return OpenAIEmbedder(config, client=_FakeClient(**kwargs))


def test_embed_preserves_order_and_dimension():
    embedder = _embedder()
    texts = [f'text-{n}' for n in range(10)]
    vectors = embedder.embed(texts)
    assert len(vectors) == len(texts)
    for n, vector in enumerate(vectors):
        assert len(vector) == _DIMS
        assert vector == [float(n)] * _DIMS   # aligned to input despite scrambled response


def test_embed_batches_large_input_in_order():
    embedder = _embedder()
    total = OpenAIEmbedder._MAX_BATCH + 5
    texts = [f'text-{n}' for n in range(total)]
    vectors = embedder.embed(texts)
    calls = embedder._get_client().calls
    assert [c['n'] for c in calls] == [OpenAIEmbedder._MAX_BATCH, 5]   # chunked, not one-per-text
    assert [v[0] for v in vectors] == [float(n) for n in range(total)]  # order kept across chunks
    assert all(c['dimensions'] == _DIMS for c in calls)


def test_embed_empty_returns_empty_without_calling_api():
    embedder = _embedder()
    assert embedder.embed([]) == []
    assert embedder._get_client().calls == []


def test_embed_dimension_mismatch_raises():
    embedder = _embedder(bad_dim=True)
    with pytest.raises(EmbeddingError):
        embedder.embed(['text-0'])


def test_embed_api_failure_raises_embedding_error():
    embedder = _embedder(boom=True)
    with pytest.raises(EmbeddingError):
        embedder.embed(['text-0'])
