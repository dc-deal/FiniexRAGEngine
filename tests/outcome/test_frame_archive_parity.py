"""The parity anchor (ISSUE_9 §8) — one envelope, two surfaces, and nothing between them.

The consumer's own rule is that their live model and their parquet projection are **one contract**:
a field is consumed in both or in neither, because a field readable in a live session but absent from
the archive means a backtest stops predicting the live run. *"Presence is reach."*

That rule has a **producer-side obligation nobody was checking.** It only holds if what we push on
the stream and what we write into the archive are the same envelope — and today they are, by
construction, because both read the one JSONB column. By construction is not by assertion: an
envelope re-validated on either path, a model default applied on one and not the other, or a field
added to the exporter's line would break their contract at *our* end, and every existing test would
stay green. `test_outcome_exporter.py` pins the line's shape; `test_stream_session.py` pins the
frame against the store. This file closes the triangle.

Needs a reachable Postgres (skipped otherwise); spends nothing.
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from finiexragengine.core.outcome.outcome_exporter import OutcomeArchiveExporter
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.core.outcome.stream_session import StreamSession
from finiexragengine.types.config_types.app_config_types import StreamConfig
from finiexragengine.types.outcome_types import (
    ArticleRef,
    RunMetadata,
    SentimentEnvelope,
    SentimentResult,
)

# What the archiver adds to a line and the wire does not carry. The exclusion set for the parity
# hash — and the only permitted difference between the two surfaces.
_ARCHIVER_ONLY = {'collected_msc', 'collected_msc_timebase'}

_TS = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
_PIPELINE = 'crypto_sentiment'


def _envelope() -> SentimentEnvelope:
    """Deliberately a *rich* envelope: provenance, a newline in `reasoning`, a populated episode id.

    A thin envelope would pass this test on defaults alone. `sources[]` is ~73 % of a production
    frame, so if any path re-serializes rather than passes through, this is where it shows.
    """
    return SentimentEnvelope(
        pipeline_id=_PIPELINE, outcome_type='sentiment_fear_greed', prompt_version='4',
        prompt_id='sentiment-crypto', prompt_hash='1c86eac137d8',
        config_fingerprint='4c0ee2c20099', trigger_reason='breaking',
        timestamp=_TS, status='success',
        result=[SentimentResult(
            symbol='BTCUSD', signal='BUY', sentiment_score=0.42, confidence=0.81,
            reasoning='bullish\nacross the board', urgency=0.77, is_breaking=True,
            basis='llm', base_currency='BTC', quote_currency='USD',
            breaking_reason='ETF inflows surge',
            breaking_episode_id=f'{_PIPELINE}:btc:2026-07-20T09:50:00+00:00',
            breaking_episode_start=True,
            evidence_as_of=int(_TS.timestamp() * 1000),
            sources=[ArticleRef(
                article_id='95a214c6fdc10e61b801f5fb352637d4',
                url='https://cryptonews.com/news/bitcoin-price-analysis/',
                title='Bitcoin Price Analysis: Can BTC Clear $80K?',
                published_at=_TS, fetched_at=_TS)])],
        metadata=RunMetadata(model='gpt-4o-mini', model_snapshot='gpt-4o-mini-2024-07-18',
                             sources_configured=7, sources_reached=6, articles_relevant=64,
                             prompt_tokens=4211, cost_usd=0.001834))


def _frame_envelope(store: OutcomeStore, database_url: str) -> Dict[str, Any]:
    """What the stream actually pushes, taken through the real session and renderer."""
    config = StreamConfig(heartbeat_seconds=1)
    session = StreamSession(
        StreamDispatcher(store, database_url),
        StreamReplay(store, config.replay_window_hours, config.max_replay_frames), config)

    async def scenario():
        frames = []
        generator = session.frames(_PIPELINE, history=1)
        try:
            async for frame in generator:
                frames.append(frame)
                if len(frames) >= 2:            # retry + the snapshot frame
                    break
        finally:
            await generator.aclose()
        return frames[1]

    frame = asyncio.run(scenario())
    return json.loads(frame.split('data: ', 1)[1].strip())


def _line(database_url: str, out_dir: Path) -> Dict[str, Any]:
    """What the archive actually receives, taken through the real exporter."""
    OutcomeArchiveExporter(database_url).export(
        out_dir, now=datetime(2026, 7, 25, tzinfo=timezone.utc))
    path = out_dir / _PIPELINE / '2026-07-20.jsonl'
    return json.loads(path.read_text(encoding='utf-8').splitlines()[0])


def test_the_frame_and_the_archive_line_carry_one_identical_envelope(clean_db, tmp_path):
    """The producer-side half of the consumer's one-contract rule.

    If this fails, a backtest over the archive stops predicting a live session — not because the
    consumer read something wrong, but because we served two different objects under one contract.
    """
    store = OutcomeStore(clean_db)
    store.save(_envelope())

    frame = _frame_envelope(store, clean_db)
    line = _line(clean_db, tmp_path)

    assert set(line) - set(frame) == _ARCHIVER_ONLY, 'the line grew a field the wire lacks'
    assert set(frame) - set(line) == set(), 'the wire grew a field the archive lacks'
    assert {key: value for key, value in line.items() if key not in _ARCHIVER_ONLY} == frame


def test_the_rich_parts_survive_both_paths_intact(clean_db, tmp_path):
    """Named separately because the assertion above would pass on two identically-empty objects.

    `sources[]` is ~73 % of a production frame and the episode fields are the ones a consumer gates
    on, so these are the parts a re-serialization would quietly alter.
    """
    store = OutcomeStore(clean_db)
    store.save(_envelope())

    frame = _frame_envelope(store, clean_db)
    line = _line(clean_db, tmp_path)

    for surface, envelope in (('frame', frame), ('line', line)):
        row = envelope['result'][0]
        assert row['sources'][0]['article_id'] == '95a214c6fdc10e61b801f5fb352637d4', surface
        assert row['sources'][0]['fetched_at'] is not None, surface
        assert row['reasoning'] == 'bullish\nacross the board', surface
        assert row['breaking_episode_id'].endswith('2026-07-20T09:50:00+00:00'), surface
        assert row['breaking_episode_start'] is True, surface
        assert row['evidence_as_of'] == int(_TS.timestamp() * 1000), surface
        assert envelope['trigger_reason'] == 'breaking', surface
        assert envelope['config_fingerprint'] == '4c0ee2c20099', surface
        assert envelope['seq'] == 1 and envelope['stream_epoch'] == 1, surface


def test_the_archiver_fields_are_the_only_permitted_difference(clean_db, tmp_path):
    """Stated as its own assertion because it is the *contract*, not an implementation detail: those
    two fields are the exclusion set for any parity hash between a line and a frame."""
    store = OutcomeStore(clean_db)
    store.save(_envelope())

    line = _line(clean_db, tmp_path)

    assert list(line)[:2] == ['collected_msc', 'collected_msc_timebase']   # prepended, in order
    assert line['collected_msc_timebase'] == 'utc'
    assert line['collected_msc'] == int(_TS.timestamp() * 1000)
