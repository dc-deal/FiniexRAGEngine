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


# --- incremental scope + the DB export flag (ISSUE_13) --------------------------------------

def test_incremental_skips_already_flagged_buckets(seeded, tmp_path):
    exporter = OutcomeArchiveExporter(seeded)
    first = exporter.export(tmp_path, incremental=True, now=_NOW)
    assert {f.bucket for f in first.files} == {'2026-07-20', '2026-07-21'}
    assert first.skipped_flagged == []
    # Second incremental run: both closed days are flagged now → nothing new, both skipped.
    second = exporter.export(tmp_path, incremental=True, now=_NOW)
    assert second.files == []
    assert second.skipped_flagged == ['crypto_sentiment/2026-07-20',
                                      'crypto_sentiment/2026-07-21']


def test_incremental_writes_only_newly_closed_days(seeded, tmp_path):
    exporter = OutcomeArchiveExporter(seeded)
    exporter.export(tmp_path, incremental=True, now=_NOW)          # flags 20 + 21
    OutcomeStore(seeded).save(_env('crypto_sentiment',
                                   datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)))
    third = exporter.export(tmp_path, incremental=True, now=_NOW)  # only the unflagged 19
    assert {f.bucket for f in third.files} == {'2026-07-19'}


def test_since_and_all_ignore_the_flag_but_still_write(seeded, tmp_path):
    exporter = OutcomeArchiveExporter(seeded)
    exporter.export(tmp_path, incremental=True, now=_NOW)          # flags 20 + 21
    # --since re-exports flagged days (ignores the flag for selection).
    since = exporter.export(tmp_path, since='2026-07-20', now=_NOW)
    assert {f.bucket for f in since.files} == {'2026-07-20', '2026-07-21'}
    # --all (no scope narrowing) likewise re-exports everything closed, flag or not.
    everything = exporter.export(tmp_path, now=_NOW)
    assert {f.bucket for f in everything.files} == {'2026-07-20', '2026-07-21'}


def test_include_open_bucket_is_never_flagged(seeded, tmp_path):
    exporter = OutcomeArchiveExporter(seeded)
    # Peek writes the open 22nd too, but must not flag it (still growing).
    peek = exporter.export(tmp_path, incremental=True, include_open=True, now=_NOW)
    assert '2026-07-22' in {f.bucket for f in peek.files}
    # Once the 22nd has closed, an incremental run still writes it — proof the peek never flagged.
    later = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
    after_close = exporter.export(tmp_path, incremental=True, now=later)
    assert '2026-07-22' in {f.bucket for f in after_close.files}


def test_auto_export_weekly_is_incremental_across_runs(seeded, tmp_path):
    cfg = WeeklyReportConfig(export_outcomes=True, export_dir=str(tmp_path))
    first = auto_export_weekly(cfg, seeded, now=_NOW)
    assert {f.bucket for f in first.files} == {'2026-07-20', '2026-07-21'}
    second = auto_export_weekly(cfg, seeded, now=_NOW)
    assert second.files == [] and second.skipped_flagged     # nothing new the next run
