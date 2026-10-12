"""OpenAIEmbedder records token usage to the cost recorder (ISSUE_23) — no DB, no API.

The OpenAI client is faked (returns embeddings + a usage block), the recorder captures
the call, so this exercises only the capture wiring.
"""
import httpx
import pytest
from openai import InternalServerError

from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder
from finiexragengine.exceptions.ragengine_errors import EmbeddingError
from finiexragengine.types.config_types.app_config_types import EmbeddingConfig


class _Item:
    def __init__(self, index, embedding):
        self.index = index
        self.embedding = embedding


class _Usage:
    def __init__(self, prompt_tokens):
        self.prompt_tokens = prompt_tokens


class _Response:
    def __init__(self, data, usage):
        self.data = data
        self.usage = usage
        self.model = 'text-embedding-3-small'   # the served model echo (ISSUE_40)


class _Embeddings:
    def create(self, model, input, dimensions, timeout=None):
        data = [_Item(i, [0.0] * dimensions) for i in range(len(input))]
        return _Response(data, _Usage(prompt_tokens=7 * len(input)))   # 7 tokens/input


class _Client:
    def __init__(self):
        self.embeddings = _Embeddings()


class _RecRecorder:
    def __init__(self):
        self.calls = []
        self.attempts = []

    def record(self, section, model, prompt_tokens, completion_tokens=0, pipeline_id=None,
               duration_ms=None, model_snapshot=None, attempt=None, outcome='ok'):
        self.calls.append((section, model, prompt_tokens, completion_tokens, pipeline_id,
                           duration_ms, model_snapshot))
        self.attempts.append((attempt, outcome))
        return 0.0


def test_embed_records_usage_when_recorder_set():
    recorder = _RecRecorder()
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=4), client=_Client(),
                              cost_recorder=recorder, section='ingest_news', pipeline_id='p')
    result = embedder.embed(['a', 'bb'])
    assert len(result.vectors) == 2
    assert len(recorder.calls) == 1
    (section, model, prompt_tokens, completion_tokens, pipeline_id,
     duration_ms, model_snapshot) = recorder.calls[0]
    assert (section, model, prompt_tokens, completion_tokens, pipeline_id) == (
        'ingest_news', 'text-embedding-3-small', 14, 0, 'p')   # 7*2
    assert duration_ms is not None and duration_ms >= 0.0       # latency sample (ISSUE_32)
    assert model_snapshot == 'text-embedding-3-small'           # served echo → drift guard (ISSUE_40)


def test_embed_without_recorder_is_silent():
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=4), client=_Client())
    assert len(embedder.embed(['x']).vectors) == 1   # no recorder, no error


# --- ISSUE_128: one row per batch request attempt -------------------------------------------

def test_every_batch_request_is_its_own_row():
    recorder = _RecRecorder()
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=4), client=_Client(),
                              cost_recorder=recorder, section='ingest_news')
    embedder.embed([f'text {n}' for n in range(OpenAIEmbedder._MAX_BATCH + 5)])
    assert [call[2] for call in recorder.calls] == [7 * OpenAIEmbedder._MAX_BATCH, 7 * 5]
    assert recorder.attempts == [(1, 'ok'), (1, 'ok')]


class _SecondBatchDown(_Embeddings):
    """The first batch answers; every request after it gets a 503."""

    def __init__(self):
        self.requests = 0

    def create(self, model, input, dimensions, timeout=None):
        self.requests += 1
        if self.requests > 1:
            raise InternalServerError('down', response=httpx.Response(
                503, request=httpx.Request('POST', 'https://api.openai.test')), body=None)
        return super().create(model, input, dimensions)


def test_a_batch_that_answered_is_kept_when_a_later_one_fails(monkeypatch):
    # The summed row used to be written at the end of embed() — a later failure took the
    # already-billed batches with it.
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    recorder = _RecRecorder()
    client = _Client()
    client.embeddings = _SecondBatchDown()
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=4), client=client,
                              cost_recorder=recorder, section='ingest_news')
    with pytest.raises(EmbeddingError):
        embedder.embed([f'text {n}' for n in range(OpenAIEmbedder._MAX_BATCH + 5)])
    assert recorder.attempts == [(1, 'ok'), (1, 'server_error'), (2, 'server_error'),
                                 (3, 'server_error')]
    assert recorder.calls[0][2] == 7 * OpenAIEmbedder._MAX_BATCH
    assert all(call[2] is None for call in recorder.calls[1:])


def test_the_embedding_client_is_bounded_and_not_retried_by_the_sdk(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    client = OpenAIEmbedder(EmbeddingConfig())._get_client()
    assert client.max_retries == 0
    assert client.timeout == EmbeddingConfig().timeout_seconds
