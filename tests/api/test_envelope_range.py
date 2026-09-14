"""`GET /v1/pipelines/{id}/envelopes` (ISSUE_9 §2) — needs a reachable Postgres, spends nothing.

The collector's catch-up path. What is asserted here is the **mapping rule** between this surface and
the stream: a condition that is terminal on the stream is a `409` here, a non-terminal marker is a
body field. Both derive from one `StreamReplay`, so the two surfaces cannot disagree about a cursor.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.endpoints.envelopes_router import build_envelopes_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry

_PIPELINE = 'crypto_sentiment'
_NOW = datetime.now(timezone.utc)


def _envelope(ts: datetime = _NOW):
    from finiexragengine.types.outcome_types import (
        RunMetadata,
        SentimentEnvelope,
        SentimentResult,
    )
    return SentimentEnvelope(
        pipeline_id=_PIPELINE, outcome_type='sentiment_fear_greed', prompt_version='4',
        timestamp=ts, status='success',
        result=[SentimentResult(symbol='BTCUSD', signal='BUY', sentiment_score=0.4,
                                confidence=0.8, reasoning='bullish')],
        metadata=RunMetadata(model='gpt-4o-mini'))


@pytest.fixture
def store(clean_db: str) -> OutcomeStore:
    return OutcomeStore(clean_db)


@pytest.fixture
def client(store: OutcomeStore) -> TestClient:
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    app = FastAPI()
    app.include_router(build_envelopes_router(StreamReplay(store, 24), registry, max_limit=3))
    return TestClient(app)


def _get(client: TestClient, query: str):
    return client.get(f'/v1/pipelines/{_PIPELINE}/envelopes?{query}')


# --- the caller errors --------------------------------------------------------------------------

def test_an_unknown_pipeline_is_a_404(client):
    assert client.get('/v1/pipelines/nope/envelopes?since=0&epoch=1').status_code == 404


@pytest.mark.parametrize('query', ['since=0', 'epoch=1', ''])
def test_since_and_epoch_are_both_required(client, query):
    """There is no other way to call this route, so the pair is mandatory rather than optional —
    a range served against an epoch the caller never held is the worst available answer."""
    assert _get(client, query).status_code == 422


# --- the range ----------------------------------------------------------------------------------

def test_the_range_returns_everything_after_the_cursor_ascending(client, store):
    for _ in range(3):
        store.save(_envelope())

    payload = _get(client, 'since=1&epoch=1').json()

    assert [env['seq'] for env in payload['envelopes']] == [2, 3]
    assert payload['head_seq'] == 3 and payload['stream_epoch'] == 1
    assert payload['truncated'] is False and payload['oldest_available_seq'] is None


def test_the_envelopes_are_the_stored_json_verbatim(client, store):
    store.save(_envelope())
    stored = store.envelopes_by_seq(_PIPELINE, 0, 1)[0]

    assert _get(client, 'since=0&epoch=1').json()['envelopes'] == [stored]


def test_the_range_is_bounded_and_the_head_says_whether_to_page_again(client, store):
    """`max_limit` is 3 in this fixture. A collector catching up after an outage will ask for
    everything, and an unbounded read of a table that grows for years is the request that makes a
    diagnostic call look like an incident."""
    for _ in range(5):
        store.save(_envelope())

    payload = _get(client, 'since=0&epoch=1').json()

    assert [env['seq'] for env in payload['envelopes']] == [1, 2, 3]
    assert payload['head_seq'] == 5                       # so the caller knows to ask again


def test_a_smaller_limit_is_honoured(client, store):
    for _ in range(5):
        store.save(_envelope())

    assert len(_get(client, 'since=0&epoch=1&limit=2').json()['envelopes']) == 2


# --- the mapping rule ---------------------------------------------------------------------------

def test_a_truncated_range_is_a_body_field_and_still_carries_data(client, store):
    """Non-terminal on the stream, so non-terminal here: the caller wants the rows it CAN have, plus
    the position it must fetch from the journal export instead."""
    for _ in range(2):
        store.save(_envelope(ts=_NOW - timedelta(days=3)))     # seq 1, 2 — outside the window
    store.save(_envelope())                                    # seq 3 — inside

    payload = _get(client, 'since=0&epoch=1').json()

    assert payload['truncated'] is True
    assert payload['oldest_available_seq'] == 3
    assert [env['seq'] for env in payload['envelopes']] == [3]


def test_an_epoch_mismatch_is_a_409_naming_both_epochs(client, store):
    """Terminal on the stream, 409 here — the cursor addresses numbers that now mean something else,
    and returning rows would be actively wrong."""
    store.save(_envelope())

    response = _get(client, 'since=0&epoch=7')

    assert response.status_code == 409
    assert response.json()['detail'] == {'code': 'epoch_changed', 'stream_epoch': 1,
                                         'previous_epoch': 7, 'head_seq': 1}


def test_a_cursor_ahead_of_the_head_is_a_409_with_its_own_code(client, store):
    """Same status, deliberately different code: this one means the CALLER rewound, and an operator
    needs to be alerted differently."""
    store.save(_envelope())

    response = _get(client, 'since=9001&epoch=1')

    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'cursor_ahead'
    assert response.json()['detail']['head_seq'] == 1
