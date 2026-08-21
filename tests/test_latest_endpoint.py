"""/latest serving semantics (ISSUE_8) — router-level with fakes, no DB, no API.

The store-backed read path: /latest serves the persisted envelope without running a
stage; a cold miss (or no store) falls back to one fresh run; a broken store degrades
to a fresh run instead of a 500; the catch-all error envelope is persisted best-effort.
"""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.endpoints.sentiment_router import build_sentiment_router
from finiexragengine.exceptions.ragengine_errors import (
    PipelineNotFoundError,
    VectorStoreError,
)
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.outcome_types import (
    RunMetadata,
    SentimentEnvelope,
    SentimentResult,
)

_TS = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)


def _envelope(reasoning: str) -> SentimentEnvelope:
    return SentimentEnvelope(
        pipeline_id='p', outcome_type='sentiment_fear_greed', prompt_version='2',
        timestamp=_TS, status='success',
        result=[SentimentResult(symbol='BTCUSD', signal='HOLD', sentiment_score=0.0,
                                confidence=0.5, reasoning=reasoning)],
        metadata=RunMetadata(model='gpt-4o-mini'))


class _FakePipeline:
    """run() serves a fresh envelope — or blows up like a broken stage."""
    def __init__(self, exc=None):
        self.runs = 0
        self.reasons = []          # why the endpoint asked for a pass (ISSUE_87)
        self._exc = exc

    def get_config(self) -> PipelineConfig:
        return PipelineConfig(
            pipeline_id='p', outcome_type='sentiment_fear_greed', market='crypto',
            symbols=[{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}], llm={'model': 'gpt-4o-mini'},
            source_set='test_news')

    def run(self, reason) -> SentimentEnvelope:
        self.runs += 1
        self.reasons.append(reason)
        if self._exc is not None:
            raise self._exc
        return _envelope('fresh run')


class _FakeRegistry:
    def __init__(self, pipeline):
        self._pipeline = pipeline

    def get(self, pipeline_id):
        if pipeline_id != 'p':
            raise PipelineNotFoundError(pipeline_id)
        return self._pipeline


class _FakeStore:
    def __init__(self, stored=None, exc=None):
        self._stored = stored
        self._exc = exc
        self.saved = []

    def get_latest(self, pipeline_id):
        if self._exc is not None:
            raise self._exc
        return self._stored

    def save(self, envelope, raw_output=None):
        self.saved.append(envelope)


def _client(pipeline, store) -> TestClient:
    app = FastAPI()
    app.include_router(build_sentiment_router(_FakeRegistry(pipeline), outcome_store=store))
    return TestClient(app)


def test_latest_serves_from_store_without_running():
    pipeline = _FakePipeline()
    client = _client(pipeline, _FakeStore(stored=_envelope('from store')))
    response = client.get('/v1/pipelines/p/latest')
    assert response.status_code == 200
    assert response.json()['result'][0]['reasoning'] == 'from store'
    assert pipeline.runs == 0                          # the read path spends nothing


def test_latest_cold_miss_answers_without_spending():
    """A GET never runs the pipeline (ISSUE_9): an empty store answers, it does not evaluate.

    The old behaviour ran one pass on a cold miss. That is a bad trade for the caller this endpoint
    exists for, and the failure it enables is not self-clearing: `_persist` swallows a failed save,
    so a broken write path leaves the store empty, every poll is another cold miss, and every cold
    miss was another paid pass — continuous spend that looks like normal operation from outside.
    """
    pipeline = _FakePipeline()
    client = _client(pipeline, _FakeStore(stored=None))
    response = client.get('/v1/pipelines/p/latest')
    assert response.status_code == 200                 # contract: always a parseable envelope
    body = response.json()
    assert body['status'] == 'error'
    assert body['errors'][0]['type'] == 'VECTOR_STORE_ERROR'
    assert 'no outcome persisted yet' in body['errors'][0]['message']
    assert [r['symbol'] for r in body['result']] == ['BTCUSD']   # every symbol still present
    assert pipeline.runs == 0, 'a GET spent money'


def test_a_cold_miss_is_not_persisted():
    """Writing the miss would make it the "latest" — every later call would serve it."""
    store = _FakeStore(stored=None)
    _client(_FakePipeline(), store).get('/v1/pipelines/p/latest')
    assert store.saved == []


def test_latest_without_store_stays_a_fresh_run():
    pipeline = _FakePipeline()
    client = _client(pipeline, store=None)
    assert client.get('/v1/pipelines/p/latest').status_code == 200
    assert pipeline.runs == 1


def test_broken_store_answers_without_spending_and_never_500s():
    """A bad minute in the database must not become a paid pass per poll (ISSUE_9)."""
    pipeline = _FakePipeline()
    client = _client(pipeline, _FakeStore(exc=VectorStoreError('db gone')))
    response = client.get('/v1/pipelines/p/latest')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'error'
    assert 'outcome store unavailable' in body['errors'][0]['message']
    assert pipeline.runs == 0, 'a store failure spent money'


def test_run_failure_persists_the_catch_all_error_envelope():
    # Error statistics aggregate from persisted envelopes — the catch-all lands too.
    store = _FakeStore(stored=None)
    client = _client(_FakePipeline(exc=RuntimeError('stage exploded')), store)
    response = client.post('/v1/pipelines/p/run')
    assert response.status_code == 200                 # contract: never a bare 500
    assert response.json()['status'] == 'error'
    assert len(store.saved) == 1 and store.saved[0].status == 'error'


def test_unknown_pipeline_is_a_plain_404():
    client = _client(_FakePipeline(), _FakeStore())
    assert client.get('/v1/pipelines/nope/latest').status_code == 404
