"""Unit tests for OpenAIEmbedder — order preservation, batching, dimension, failure.

The OpenAI client is faked, so these run offline with no API key. The fake returns
each batch's items in scrambled order (with correct `.index`) to prove the embedder
re-aligns to the input order.
"""
import random

import pytest

pytest.importorskip('openai')
import httpx  # noqa: E402
from openai import BadRequestError, OpenAIError  # noqa: E402

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
    vectors = embedder.embed(texts).vectors
    assert len(vectors) == len(texts)
    for n, vector in enumerate(vectors):
        assert len(vector) == _DIMS
        assert vector == [float(n)] * _DIMS   # aligned to input despite scrambled response


def test_embed_batches_large_input_in_order():
    embedder = _embedder()
    total = OpenAIEmbedder._MAX_BATCH + 5
    texts = [f'text-{n}' for n in range(total)]
    vectors = embedder.embed(texts).vectors
    calls = embedder._get_client().calls
    assert [c['n'] for c in calls] == [OpenAIEmbedder._MAX_BATCH, 5]   # chunked, not one-per-text
    assert [v[0] for v in vectors] == [float(n) for n in range(total)]  # order kept across chunks
    assert all(c['dimensions'] == _DIMS for c in calls)


def test_embed_empty_returns_empty_without_calling_api():
    embedder = _embedder()
    assert embedder.embed([]).vectors == []
    assert embedder._get_client().calls == []


def test_embed_dimension_mismatch_raises():
    embedder = _embedder(bad_dim=True)
    with pytest.raises(EmbeddingError):
        embedder.embed(['text-0'])


def test_embed_api_failure_raises_embedding_error():
    embedder = _embedder(boom=True)
    with pytest.raises(EmbeddingError):
        embedder.embed(['text-0'])


# --- ISSUE_79: fitting to the input limit + isolating a rejected item -----------------------


class _PoisonEmbeddings:
    """Rejects any batch containing a poisoned text — the 2026-08-11 production shape.

    The provider answers 400 for the *whole* request when one input is unacceptable; it does not
    embed the rest. That is what made a single over-long article cost an entire ingest pass.
    """

    def __init__(self, parent: '_PoisonClient', poisoned: set) -> None:
        self._parent = parent
        self._poisoned = poisoned

    def create(self, model: str, input: list, dimensions: int) -> _Response:
        self._parent.calls.append(list(input))
        offenders = [text for text in input if text in self._poisoned]
        if offenders:
            raise BadRequestError(
                message=f"Invalid 'input[{input.index(offenders[0])}]': maximum input length "
                        'is 8192 tokens.',
                response=httpx.Response(400, request=httpx.Request('POST', 'https://api.test')),
                body=None)
        data = [_Item(i, [float(int(text.split('-')[1]))] * dimensions)
                for i, text in enumerate(input)]
        return _Response(data)


class _PoisonClient:
    def __init__(self, poisoned: set) -> None:
        self.calls: list = []
        self.embeddings = _PoisonEmbeddings(self, poisoned)


def test_a_rejected_input_is_isolated_and_the_rest_still_embed():
    """The regression for the 376-failure loop.

    One unacceptable item must cost exactly itself. Before ISSUE_79 the 400 propagated out of
    `Ingestor.run` and killed the pass, so *no* article from that source was stored — and the
    offender, never stored, came back every pass until its feed dropped it.
    """
    texts = [f'text-{n}' for n in range(8)]
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=_DIMS),
                              client=_PoisonClient({'text-5'}))
    result = embedder.embed(texts)

    assert result.rejected == [5]
    assert result.vectors[5] is None
    for n in range(8):
        if n != 5:
            assert result.vectors[n] == [float(n)] * _DIMS   # every other item still embedded


def test_two_poison_items_are_both_isolated():
    texts = [f'text-{n}' for n in range(8)]
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=_DIMS),
                              client=_PoisonClient({'text-1', 'text-6'}))
    result = embedder.embed(texts)
    assert sorted(result.rejected) == [1, 6]
    assert sum(1 for vector in result.vectors if vector is not None) == 6


def test_isolation_bisects_rather_than_retrying_one_by_one():
    # Bounded cost is what makes this safe on a 256-item batch: halving, not a linear sweep.
    texts = [f'text-{n}' for n in range(8)]
    client = _PoisonClient({'text-5'})
    OpenAIEmbedder(EmbeddingConfig(dimensions=_DIMS), client=client).embed(texts)
    assert len(client.calls) < 8, client.calls        # a per-item retry would be 8+ calls


def test_a_quota_error_is_not_bisected():
    # Narrow trigger: only a 400 means "this batch holds something unacceptable". A quota stop
    # must raise immediately — bisecting it would multiply a temporary outage.
    embedder = _embedder(boom=True)
    with pytest.raises(EmbeddingError):
        embedder.embed(['text-0', 'text-1'])


class _EchoEmbeddings:
    """Returns a zero vector per input and records exactly what it was sent."""

    def __init__(self, parent: '_EchoClient') -> None:
        self._parent = parent

    def create(self, model: str, input: list, dimensions: int) -> _Response:
        self._parent.sent.extend(input)
        return _Response([_Item(i, [0.0] * dimensions) for i in range(len(input))])


class _EchoClient:
    def __init__(self) -> None:
        self.sent: list = []
        self.embeddings = _EchoEmbeddings(self)


def test_over_long_input_is_truncated_before_it_is_sent():
    original = 'filler ' * 200
    client = _EchoClient()
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=_DIMS, max_input_tokens=20),
                              client=client)
    result = embedder.embed([original])

    assert result.input_tokens[0] == 20                 # fitted exactly to the limit
    assert result.truncated_tokens[0] > 0               # and the cut is reported, not silent
    assert result.truncated_count == 1
    # The point of fitting *before* the call: the provider never sees the over-long text, so the
    # 400 that used to kill the pass cannot happen in the first place.
    assert len(client.sent[0]) < len(original)


def test_token_mismatch_warns_but_does_not_fail(caplog):
    # The canary: our count vs the provider's. It must never gate the pass.
    class _Usage:
        prompt_tokens = 99999

    class _UsageResponse(_Response):
        usage = _Usage()

    class _Embeddings2(_Embeddings):
        def create(self, model, input, dimensions):
            base = super().create(model, input, dimensions)
            return _UsageResponse(base.data)

    client = _FakeClient()
    client.embeddings = _Embeddings2(client, False, False)
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=_DIMS), client=client)
    with caplog.at_level('WARNING'):
        result = embedder.embed(['text-1'])
    assert result.vectors[0] is not None               # the pass survives
    assert any('token count mismatch' in record.message for record in caplog.records)
