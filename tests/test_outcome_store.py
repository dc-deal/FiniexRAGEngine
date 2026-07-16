"""Tests for the OutcomeStore (ISSUE_8/36) — needs a reachable Postgres (skipped
otherwise), no API budget.

Runs against the canonical `outcomes` table inside the isolated, migration-built test schema
(the `clean_db` fixture, ISSUE_14) — so this exercises the real schema, not hand-written test DDL.
"""
from datetime import datetime, timedelta, timezone

import pytest

from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.types.outcome_types import (
    RunError,
    RunMetadata,
    SentimentEnvelope,
    SentimentResult,
)

_TS = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)


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
def store(clean_db: str) -> OutcomeStore:
    return OutcomeStore(clean_db)


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
