"""Tests for the OutcomeStore (ISSUE_8/36) — needs a reachable Postgres (skipped
otherwise), no API budget. Mirrors the cost-recorder test setup: its own table,
dropped before and after.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip('psycopg')
import psycopg  # noqa: E402

from finiexragengine.core.store.outcome_store import OutcomeStore  # noqa: E402
from finiexragengine.exceptions.ragengine_errors import VectorStoreError  # noqa: E402
from finiexragengine.types.outcome_types import (  # noqa: E402
    RunError,
    RunMetadata,
    SentimentEnvelope,
    SentimentResult,
)

_TABLE = 'outcomes_test'
_TS = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)


def _dsn() -> str:
    return os.environ.get(
        'DATABASE_URL', 'postgresql://ragengine:ragengine@127.0.0.1:5433/ragengine')


def _envelope(pipeline_id='p', ts=_TS, status='success') -> SentimentEnvelope:
    result = [] if status == 'error' else [SentimentResult(
        symbol='BTCUSD', signal='BUY', sentiment_score=0.4, confidence=0.8,
        reasoning='bullish')]
    errors = ([RunError(type='LLM_TIMEOUT', message='too slow', timestamp=ts)]
              if status == 'error' else [])
    return SentimentEnvelope(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed',
        prompt_version='2', prompt_id='sentiment-crypto', prompt_hash='1c86eac137d8',
        timestamp=ts, status=status, result=result,
        metadata=RunMetadata(model='gpt-4o-mini',
                             model_snapshot='gpt-4o-mini-2024-07-18'),
        errors=errors)


@pytest.fixture
def store():
    def _drop() -> None:
        with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {_TABLE}')
    try:
        _drop()
        outcome_store = OutcomeStore(_dsn(), table=_TABLE)
    except (psycopg.Error, VectorStoreError) as exc:
        pytest.skip(f'PostgreSQL not available: {exc}')
    yield outcome_store
    _drop()


def test_save_get_latest_roundtrip_is_typed_and_lossless(store):
    envelope = _envelope()
    store.save(envelope)
    loaded = store.get_latest('p')
    assert isinstance(loaded, SentimentEnvelope)
    # The store returns exactly what was persisted — the source-of-truth property.
    assert loaded.model_dump() == envelope.model_dump()


def test_get_latest_none_when_nothing_stored(store):
    assert store.get_latest('never-ran') is None


def test_latest_is_the_newest_by_timestamp(store):
    store.save(_envelope(ts=_TS))
    store.save(_envelope(ts=_TS + timedelta(minutes=10)))
    loaded = store.get_latest('p')
    assert loaded.timestamp == _TS + timedelta(minutes=10)


def test_pipelines_are_isolated(store):
    store.save(_envelope(pipeline_id='a'))
    store.save(_envelope(pipeline_id='b', ts=_TS + timedelta(minutes=1)))
    assert store.get_latest('a').pipeline_id == 'a'
    assert store.get_latest('b').pipeline_id == 'b'


def test_raw_output_rides_next_to_the_envelope(store):
    # ISSUE_36: raw model output, same key as the envelope — and absent stays None.
    raw = {'BTCUSD': {'signal': 'BUY', 'sentiment_score': 0.4, 'confidence': 0.8,
                      'reasoning': 'bullish', 'urgency': 0.1}}
    store.save(_envelope(), raw_output=raw)
    assert store.get_latest_raw_output('p') == raw
    store.save(_envelope(ts=_TS + timedelta(minutes=10)))   # a later no-raw pass
    assert store.get_latest_raw_output('p') is None


def test_error_envelope_persists_for_error_statistics(store):
    # Error statistics aggregate from persisted envelopes — error passes are rows too.
    store.save(_envelope(status='error'))
    loaded = store.get_latest('p')
    assert loaded.status == 'error' and loaded.result == []
    assert loaded.errors[0].type == 'LLM_TIMEOUT'
