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

def test_save_stamps_the_stream_position_into_the_envelope(store):
    """The stamp has to land *in the JSON*, not just in a column (ISSUE_9).

    `OutcomeExporter` builds the archive line from the envelope, so a `seq` living only in a table
    column would never reach the consumer — and the archive is what their backtest orders by.
    """
    envelope = _envelope()
    store.save(envelope)

    assert (envelope.seq, envelope.stream_epoch) == (1, 1)
    assert envelope.available_msc is not None
    # `timestamp` is the analysis wall-clock at assembly; `available_msc` is the store write, so it
    # is at or after it — the consumer gates no-look-ahead on the second, never the first.
    assert envelope.available_msc >= int(envelope.timestamp.timestamp() * 1000)

    loaded = store.get_latest('p')
    assert (loaded.seq, loaded.stream_epoch) == (1, 1)
    assert loaded.available_msc == envelope.available_msc


def test_each_stream_is_sequenced_independently(store):
    """A fan-out variant is its own pipeline_id and therefore its own series (ISSUE_42/ISSUE_9)."""
    for _ in range(2):
        store.save(_envelope(pipeline_id='crypto_sentiment'))
    store.save(_envelope(pipeline_id='crypto_sentiment_4o_enhanced'))

    assert store.get_latest('crypto_sentiment').seq == 2
    assert store.get_latest('crypto_sentiment_4o_enhanced').seq == 1


def test_an_unsequenced_archive_line_still_loads(store):
    """Envelopes written before ISSUE_9 carry no position; absent must parse, never raise.

    The envelope contract's "always parseable" rule spans the change — a consumer reads absent as
    "produced before this existed", which is the only thing it can mean.
    """
    old = {'schema_version': '1.0', 'pipeline_id': 'p', 'outcome_type': 'sentiment_fear_greed',
           'prompt_version': '2', 'timestamp': '2026-07-12T10:00:00Z', 'status': 'success',
           'result': [], 'metadata': {'model': 'gpt-4o-mini'}}
    parsed = SentimentEnvelope(**old)
    assert (parsed.seq, parsed.stream_epoch, parsed.available_msc) == (None, None, None)

def test_journal_id_identifies_the_store_not_the_process(clean_db):
    """A stable fingerprint of the database the engine writes into (ISSUE_9).

    The consumer's release certificate has to record which producer it was taken against, or it is
    unfalsifiable a month later. Two engines pointing at one database are one series, so the identity
    that matters is the store's — hence the name and the derivation.
    """
    from finiexragengine.core.outcome.outcome_store import OutcomeStore
    store = OutcomeStore(clean_db)
    journal_id = store.journal_id()
    assert journal_id and len(journal_id) == 12
    # Stable across calls and across instances pointed at the same database — that is the property
    # a certificate rests on.
    assert store.journal_id() == journal_id
    assert OutcomeStore(clean_db).journal_id() == journal_id


def test_an_unreachable_store_reports_no_journal_id():
    """"Cannot be established" is an answer; a substitute derived from the DSN would collide.

    A dev container and a server deployment can easily share `host:port/database`, so a DSN-derived
    identity would claim two different journals are one — the failure the field exists to prevent.
    """
    from finiexragengine.core.outcome.outcome_store import OutcomeStore
    assert OutcomeStore('postgresql://nobody:nope@nowhere:5432/none').journal_id() is None
