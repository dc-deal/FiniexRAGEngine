"""`GET /v1/pipelines/{id}/archive` and `/archive/days` — needs a reachable Postgres, no spend.

The series beyond the stream's replay window, in bounded windows. What is asserted is the design:
bounded twice and refused rather than cut; the export's line, byte for byte; and read-only against
the incremental handover — `archive_export_log` must look the same after any number of requests.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.endpoints.archive_router import build_archive_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.outcome.outcome_exporter import OutcomeArchiveExporter
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.outcome_types import RunMetadata, SentimentEnvelope, SentimentResult

_PIPELINE = 'crypto_sentiment'
_DAY = datetime(2026, 9, 10, tzinfo=timezone.utc)
_NOW = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)      # the clock the router is handed


def _envelope(ts: datetime) -> SentimentEnvelope:
    return SentimentEnvelope(
        pipeline_id=_PIPELINE, outcome_type='sentiment_fear_greed', prompt_version='5',
        timestamp=ts, status='success',
        result=[SentimentResult(symbol='BTCUSD', signal='SELL', sentiment_score=-0.4,
                                confidence=0.7, reasoning='ECB holds rates')],
        metadata=RunMetadata(model='gpt-4o-mini'))


def _client(dsn: str, max_lines: int = 500) -> TestClient:
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    app = FastAPI()
    app.include_router(build_archive_router(OutcomeArchiveExporter(dsn), registry,
                                            max_lines=max_lines, now=lambda: _NOW))
    return TestClient(app)


@pytest.fixture
def store(clean_db: str) -> OutcomeStore:
    return OutcomeStore(clean_db)


@pytest.fixture
def client(clean_db: str) -> TestClient:
    return _client(clean_db)


def _window(client: TestClient, start: datetime, end: datetime,
            pipeline: str = _PIPELINE) -> httpx.Response:
    return client.get(f'/v1/pipelines/{pipeline}/archive',
                      params={'from': start.isoformat(), 'to': end.isoformat()})


def _lines(response: httpx.Response) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines()]


def _export_log(dsn: str) -> List[Tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('SELECT stream_id, bucket, boundary, lines, exported_at '
                    'FROM archive_export_log ORDER BY stream_id, bucket, boundary')
        return cur.fetchall()


# --- the caller errors --------------------------------------------------------------------------

def test_an_unknown_pipeline_is_a_404_on_both_routes(client: TestClient) -> None:
    assert _window(client, _DAY, _DAY + timedelta(hours=1), pipeline='nope').status_code == 404
    assert client.get('/v1/pipelines/nope/archive/days').status_code == 404


def test_the_window_end_must_come_after_its_start(client: TestClient) -> None:
    assert _window(client, _DAY, _DAY).status_code == 400
    assert _window(client, _DAY + timedelta(hours=1), _DAY).status_code == 400


def test_a_window_longer_than_the_span_cap_is_refused(client: TestClient) -> None:
    response = _window(client, _DAY, _DAY + timedelta(hours=25))

    assert response.status_code == 422
    assert response.json()['detail'] == {'code': 'span_too_long', 'max_span_hours': 24,
                                         'requested_hours': 25.0}


# --- the window ---------------------------------------------------------------------------------

def test_the_window_is_start_inclusive_end_exclusive_and_ascending(
        client: TestClient, store: OutcomeStore) -> None:
    for minute in (20, 0, 10):
        store.save(_envelope(_DAY + timedelta(hours=12, minutes=minute)))

    response = _window(client, _DAY + timedelta(hours=12), _DAY + timedelta(hours=12, minutes=20))

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/x-ndjson')
    assert response.headers['x-archive-lines'] == '2'
    assert [line['timestamp'][11:16] for line in _lines(response)] == ['12:00', '12:10']


def test_a_naive_instant_is_read_as_utc(client: TestClient, store: OutcomeStore) -> None:
    store.save(_envelope(_DAY + timedelta(hours=12)))

    response = client.get(f'/v1/pipelines/{_PIPELINE}/archive',
                          params={'from': '2026-09-10T11:00:00', 'to': '2026-09-10T13:00:00'})

    assert response.headers['x-archive-lines'] == '1'
    assert response.headers['x-archive-from'] == '2026-09-10T11:00:00+00:00'


def test_a_full_day_equals_the_exported_file_line_for_line(
        client: TestClient, store: OutcomeStore, clean_db: str, tmp_path: Path) -> None:
    """One line builder, two surfaces: the HTTP read and the file handover cannot disagree."""
    for hour in (3, 12, 23):
        store.save(_envelope(_DAY + timedelta(hours=hour)))
    OutcomeArchiveExporter(clean_db).export(tmp_path, day='2026-09-10', now=_NOW)
    exported = (tmp_path / _PIPELINE / '2026-09-10.jsonl').read_text(encoding='utf-8')

    response = _window(client, _DAY, _DAY + timedelta(days=1))

    assert response.text == exported


def test_more_lines_than_the_cap_are_refused_never_cut(
        clean_db: str, store: OutcomeStore) -> None:
    """Complete or refused: a partial answer is the one a caller can mistake for the whole."""
    for minute in (0, 10, 20):
        store.save(_envelope(_DAY + timedelta(hours=12, minutes=minute)))

    response = _window(_client(clean_db, max_lines=2), _DAY, _DAY + timedelta(days=1))

    assert response.status_code == 422
    assert response.json()['detail'] == {'code': 'too_many_lines', 'max_lines': 2,
                                         'lines_at_least': 3, 'hint': 'narrow the window'}


def test_an_empty_window_is_an_empty_answer_not_an_error(client: TestClient) -> None:
    response = _window(client, _DAY, _DAY + timedelta(hours=1))

    assert response.status_code == 200
    assert response.text == '' and response.headers['x-archive-lines'] == '0'


def test_a_window_reaching_into_the_present_says_it_is_still_growing(
        client: TestClient) -> None:
    growing = _window(client, _NOW - timedelta(hours=1), _NOW + timedelta(hours=1))
    closed = _window(client, _NOW - timedelta(hours=2), _NOW)

    assert growing.headers['x-archive-window-open'] == 'true'
    assert closed.headers['x-archive-window-open'] == 'false'


# --- the day index ------------------------------------------------------------------------------

def test_the_day_index_counts_lines_and_reads_the_handover_flag(
        client: TestClient, store: OutcomeStore, clean_db: str, tmp_path: Path) -> None:
    for hour in (3, 12):
        store.save(_envelope(_DAY + timedelta(hours=hour)))
    store.save(_envelope(_NOW - timedelta(hours=1)))                 # today, still growing
    OutcomeArchiveExporter(clean_db).export(tmp_path, day='2026-09-10', now=_NOW)

    days = client.get(f'/v1/pipelines/{_PIPELINE}/archive/days').json()

    assert days == {'pipeline_id': _PIPELINE, 'days': [
        {'day': '2026-09-10', 'lines': 2, 'open': False, 'exported': True},
        {'day': '2026-09-11', 'lines': 1, 'open': True, 'exported': False}]}


# --- the handover stays intact ------------------------------------------------------------------

def test_neither_route_touches_the_export_log(
        client: TestClient, store: OutcomeStore, clean_db: str, tmp_path: Path) -> None:
    """The incremental export decides from `archive_export_log`; a pull over HTTP must never mark a
    day as handed over, refresh a stamp, or remove a row — or the next handover skips real days."""
    for hour in (3, 12):
        store.save(_envelope(_DAY + timedelta(hours=hour)))
    OutcomeArchiveExporter(clean_db).export(tmp_path, day='2026-09-10', now=_NOW)
    before = _export_log(clean_db)

    for _ in range(3):
        _window(client, _DAY, _DAY + timedelta(days=1))
        _window(client, _NOW - timedelta(hours=2), _NOW + timedelta(hours=1))
        client.get(f'/v1/pipelines/{_PIPELINE}/archive/days')

    assert before and _export_log(clean_db) == before
