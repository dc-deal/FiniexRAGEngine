"""`GET /v1/stream/{pipeline_id}` — the transport's HTTP surface (ISSUE_9 §3).

**Only requests that terminate by themselves are driven over HTTP here.** An SSE stream never ends,
and a test client cannot close one before the server's response generator finishes — it deadlocks.
The frame sequence is therefore asserted against the generator in
`tests/outcome/test_stream_session.py`; what this file covers is what only the HTTP layer decides:
the status codes, the parameter refusals, and the two control codes that close the connection.

Needs a reachable Postgres (skipped otherwise); spends nothing.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.endpoints.stream_router import build_stream_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.config_types.app_config_types import StreamConfig
from finiexragengine.types.outcome_types import RunMetadata, SentimentEnvelope, SentimentResult

_PIPELINE = 'crypto_sentiment'          # a real constellation, so the registry resolves it
_CHANNEL = 'test_stream_endpoint'


def _envelope() -> SentimentEnvelope:
    return SentimentEnvelope(
        pipeline_id=_PIPELINE, outcome_type='sentiment_fear_greed', prompt_version='4',
        timestamp=datetime.now(timezone.utc), status='success',
        result=[SentimentResult(symbol='BTCUSD', signal='BUY', sentiment_score=0.4,
                                confidence=0.8, reasoning='bullish')],
        metadata=RunMetadata(model='gpt-4o-mini'))


def _app(clean_db: str, config: StreamConfig) -> FastAPI:
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    store = OutcomeStore(clean_db, notify_channel=_CHANNEL)
    app = FastAPI()
    app.include_router(build_stream_router(
        StreamDispatcher(store, clean_db, notify_channel=_CHANNEL),
        StreamReplay(store, config.replay_window_hours), registry, config))
    return app


@pytest.fixture
def store(clean_db: str) -> OutcomeStore:
    return OutcomeStore(clean_db, notify_channel=_CHANNEL)


@pytest.fixture
def client(clean_db: str) -> TestClient:
    return TestClient(_app(clean_db, StreamConfig(notify_channel=_CHANNEL)))


def _payloads(lines: List[str]) -> List[Dict[str, Any]]:
    return [json.loads(line[len('data: '):]) for line in lines if line.startswith('data: ')]


# --- the status codes ---------------------------------------------------------------------------

def test_an_unknown_pipeline_is_a_404_never_an_empty_stream(client):
    """"Exists but idle" and "does not exist" are different operator situations, and a client that
    cannot tell them apart waits forever on a typo. The 404 is the router's answer because the
    pipeline is a PATH segment — which is also what makes the grant checkable."""
    assert client.get('/v1/stream/no_such_pipeline').status_code == 404


def test_a_disabled_transport_answers_503_rather_than_an_empty_stream(clean_db):
    """An operator switching the stream off is a temporary condition, not a missing route: 503 says
    "not now", where a 404 would say "never" and send a consumer looking for a typo."""
    client = TestClient(_app(clean_db, StreamConfig(enabled=False, notify_channel=_CHANNEL)))

    assert client.get(f'/v1/stream/{_PIPELINE}').status_code == 503


def test_history_and_since_together_are_refused(client):
    """One is the connect snapshot, the other a resync. A silent precedence would make the caller's
    intent unknowable from the request."""
    response = client.get(f'/v1/stream/{_PIPELINE}?history=5&since=3&epoch=1')

    assert response.status_code == 400
    assert 'mutually exclusive' in response.json()['detail']


@pytest.mark.parametrize('query', ['since=3', 'epoch=1'])
def test_since_and_epoch_are_refused_apart(client, query):
    """A cursor is `(epoch, seq)`. Serving `since+1..` of a series the caller may not be on is worse
    than rejecting the request — and accepting a parameter then ignoring it is worse still."""
    response = client.get(f'/v1/stream/{_PIPELINE}?{query}')

    assert response.status_code == 400
    assert 'cursor is (epoch, seq)' in response.json()['detail']


def test_a_negative_history_is_refused_by_the_schema(client):
    assert client.get(f'/v1/stream/{_PIPELINE}?history=-1').status_code == 422


# --- the terminal control codes, over a real socket ---------------------------------------------

def _closing_stream(client: TestClient, url: str) -> List[str]:
    """Read a stream that ends on its own — the only kind an HTTP client can read to completion."""
    with client.stream('GET', url) as response:
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/event-stream')
        return [line for line in response.iter_lines() if line]


def test_an_epoch_mismatch_closes_the_connection_over_http(client, store):
    """The end-to-end shape of the terminal path: one control frame, then the socket closes — which
    is what gives the consumer's boot bridge exactly one resync path."""
    store.save(_envelope())

    lines = _closing_stream(client, f'/v1/stream/{_PIPELINE}?since=0&epoch=7')

    assert lines[0] == 'retry: 5000'
    assert lines[1] == 'event: control'
    assert _payloads(lines) == [{'code': 'epoch_changed', 'stream_epoch': 1,
                                 'previous_epoch': 7, 'head_seq': 1}]


def test_a_cursor_ahead_of_the_head_closes_too(client, store):
    """Same remedy as `epoch_changed`, deliberately different diagnosis: this one means the CONSUMER
    rewound, and an operator needs to be alerted differently."""
    store.save(_envelope())

    lines = _closing_stream(client, f'/v1/stream/{_PIPELINE}?since=9001&epoch=1')

    assert _payloads(lines) == [{'code': 'cursor_ahead', 'stream_epoch': 1,
                                 'requested_since': 9001, 'head_seq': 1}]


def test_a_terminal_frame_ends_with_the_blank_line_that_dispatches_it(client, store):
    """A conforming client dispatches an event on a blank line, so a terminal frame without one is
    never delivered at all — the connection would close on a buffered event."""
    store.save(_envelope())

    with client.stream('GET', f'/v1/stream/{_PIPELINE}?since=9001&epoch=1') as response:
        body = response.read().decode()

    assert body.endswith('\n\n')
