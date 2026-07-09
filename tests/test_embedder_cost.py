"""OpenAIEmbedder records token usage to the cost recorder (ISSUE_23) — no DB, no API.

The OpenAI client is faked (returns embeddings + a usage block), the recorder captures
the call, so this exercises only the capture wiring.
"""
from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder
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


class _Embeddings:
    def create(self, model, input, dimensions):
        data = [_Item(i, [0.0] * dimensions) for i in range(len(input))]
        return _Response(data, _Usage(prompt_tokens=7 * len(input)))   # 7 tokens/input


class _Client:
    def __init__(self):
        self.embeddings = _Embeddings()


class _RecRecorder:
    def __init__(self):
        self.calls = []

    def record(self, section, model, prompt_tokens, completion_tokens=0, pipeline_id=None):
        self.calls.append((section, model, prompt_tokens, completion_tokens, pipeline_id))
        return 0.0


def test_embed_records_usage_when_recorder_set():
    recorder = _RecRecorder()
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=4), client=_Client(),
                              cost_recorder=recorder, section='ingest_news', pipeline_id='p')
    vectors = embedder.embed(['a', 'bb'])
    assert len(vectors) == 2
    assert recorder.calls == [('ingest_news', 'text-embedding-3-small', 14, 0, 'p')]  # 7*2


def test_embed_without_recorder_is_silent():
    embedder = OpenAIEmbedder(EmbeddingConfig(dimensions=4), client=_Client())
    assert len(embedder.embed(['x'])) == 1   # no recorder, no error
