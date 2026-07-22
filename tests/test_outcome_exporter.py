"""Outcome archive exporter (ISSUE_13) — DB journal → rotated JSONL.

Closed-days-only, idempotent, per-stream layout. Needs a reachable Postgres (skipped
otherwise, via the clean_db fixture), no API budget.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from finiexragengine.core.outcome.outcome_exporter import (
    OutcomeArchiveExporter,
    auto_export_weekly,
)
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.types.config_types.app_config_types import WeeklyReportConfig
from finiexragengine.types.outcome_types import (
    RunMetadata,
    SentimentEnvelope,
    SentimentResult,
)

_NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)   # morning of the 22nd (UTC)


def _env(pipeline_id: str, ts: datetime) -> SentimentEnvelope:
    return SentimentEnvelope(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed', prompt_version='2',
        prompt_id='sentiment-crypto', prompt_hash='1c86eac137d8', timestamp=ts,
        status='success',
        result=[SentimentResult(symbol='BTCUSD', signal='BUY', sentiment_score=0.4,
                                confidence=0.8, reasoning='r')],
        metadata=RunMetadata(model='gpt-4o-mini', model_snapshot='gpt-4o-mini-2024-07-18'))


@pytest.fixture
def seeded(clean_db: str) -> str:
    store = OutcomeStore(clean_db)
    # two rows on the 20th, one on the 21st (both closed by _NOW), one on the 22nd (open).
    store.save(_env('crypto_sentiment', datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)))
    store.save(_env('crypto_sentiment', datetime(2026, 7, 20, 10, 10, tzinfo=timezone.utc)))
    store.save(_env('crypto_sentiment', datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)))
    store.save(_env('crypto_sentiment', datetime(2026, 7, 22, 7, 0, tzinfo=timezone.utc)))
    return clean_db


def test_exports_closed_days_and_skips_the_open_one(seeded, tmp_path):
    result = OutcomeArchiveExporter(seeded).export(tmp_path, now=_NOW)
    written = {f.bucket: f for f in result.files}
    assert set(written) == {'2026-07-20', '2026-07-21'}     # 22nd still open → skipped
    assert result.skipped_open == ['2026-07-22']
    assert written['2026-07-20'].lines == 2
    assert written['2026-07-21'].lines == 1
    assert (tmp_path / 'crypto_sentiment' / '2026-07-20.jsonl').exists()


def test_line_shape_and_collected_msc_equals_ts(seeded, tmp_path):
    OutcomeArchiveExporter(seeded).export(tmp_path, now=_NOW)
    lines = (tmp_path / 'crypto_sentiment' / '2026-07-20.jsonl').read_text().splitlines()
    first = json.loads(lines[0])
    assert list(first)[0] == 'collected_msc'                # prepended to the envelope
    ts = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    assert first['collected_msc'] == int(ts.timestamp() * 1000)
    assert first['pipeline_id'] == 'crypto_sentiment'
    msc = [json.loads(line)['collected_msc'] for line in lines]
    assert msc == sorted(msc) and len(set(msc)) == len(msc)  # strictly increasing, unique


def test_reexport_of_a_closed_day_is_byte_identical(seeded, tmp_path):
    exporter = OutcomeArchiveExporter(seeded)
    exporter.export(tmp_path, now=_NOW)
    path = tmp_path / 'crypto_sentiment' / '2026-07-20.jsonl'
    first = path.read_bytes()
    exporter.export(tmp_path, now=_NOW)                     # re-run: no redundancy
    assert path.read_bytes() == first


def test_include_open_writes_the_current_bucket(seeded, tmp_path):
    result = OutcomeArchiveExporter(seeded).export(tmp_path, now=_NOW, include_open=True)
    assert '2026-07-22' in {f.bucket for f in result.files}
    assert result.skipped_open == []


def test_day_filter_exports_only_that_bucket(seeded, tmp_path):
    result = OutcomeArchiveExporter(seeded).export(tmp_path, now=_NOW, day='2026-07-21')
    assert {f.bucket for f in result.files} == {'2026-07-21'}


def test_empty_store_exports_nothing_cleanly(clean_db, tmp_path):
    result = OutcomeArchiveExporter(clean_db).export(tmp_path, now=_NOW)
    assert result.files == [] and result.total_lines == 0


def test_auto_export_weekly_dumps_closed_days_when_enabled(seeded, tmp_path):
    # The weekly-report coupling: default-on, writes the same closed-day layout as export_cli.
    cfg = WeeklyReportConfig(export_outcomes=True, export_dir=str(tmp_path))
    result = auto_export_weekly(cfg, seeded, now=_NOW)
    assert result is not None
    assert {f.bucket for f in result.files} == {'2026-07-20', '2026-07-21'}   # open 22nd skipped
    assert (tmp_path / 'crypto_sentiment' / '2026-07-20.jsonl').exists()


def test_auto_export_weekly_is_silent_when_disabled(seeded, tmp_path):
    cfg = WeeklyReportConfig(export_outcomes=False, export_dir=str(tmp_path))
    assert auto_export_weekly(cfg, seeded, now=_NOW) is None
    assert not (tmp_path / 'crypto_sentiment').exists()          # nothing written
