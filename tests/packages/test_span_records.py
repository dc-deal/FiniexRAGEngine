"""Signal packages — the starts and outages a manifest carries beside its members (real PostgreSQL).

Starts are read from `config_generations` (kept forever); outages are the gaps in the 60 s
`resource_samples` (kept 14 days). Days the record does not reach are `days_missing` — never "none".
"""
from datetime import datetime, timedelta, timezone

import psycopg

from finiexragengine.core.packages.span_records import OUTAGE_GAP_SECONDS, outages, process_starts

_START = datetime(2026, 7, 20, tzinfo=timezone.utc)
_END = datetime(2026, 7, 22, tzinfo=timezone.utc)


def _samples(dsn: str, start: datetime, end: datetime, step_s: int = 60) -> None:
    moments = []
    cursor = start
    while cursor < end:
        moments.append((cursor,))
        cursor += timedelta(seconds=step_s)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.executemany('INSERT INTO resource_samples (ts, rss_mb) VALUES (%s, 100)', moments)


def test_a_gap_longer_than_the_threshold_is_an_outage(clean_db):
    _samples(clean_db, _START, _START + timedelta(hours=10))
    _samples(clean_db, _START + timedelta(hours=10, minutes=30), _END)       # 30 minutes down
    record = outages(clean_db, _START, _END, now=_END + timedelta(hours=1))

    assert record['count'] == 1 and record['days_missing'] == []
    outage = record['records'][0]
    assert outage['from'].startswith('2026-07-20T09:59') and outage['until'].startswith('2026-07-20T10:30')
    assert outage['seconds'] == 31 * 60


def test_a_restart_shorter_than_the_threshold_is_not_an_outage(clean_db):
    _samples(clean_db, _START, _START + timedelta(hours=10))
    _samples(clean_db, _START + timedelta(hours=10, seconds=OUTAGE_GAP_SECONDS - 60), _END)
    assert outages(clean_db, _START, _END, now=_END)['count'] == 0


def test_days_before_the_oldest_sample_are_missing_not_clean(clean_db):
    _samples(clean_db, _START + timedelta(days=1), _END)
    record = outages(clean_db, _START, _END, now=_END)
    assert record == {'count': 0, 'days_missing': ['2026-07-20'], 'records': []}


def test_an_outage_still_running_at_the_span_end_is_cut_there(clean_db):
    _samples(clean_db, _START, _START + timedelta(hours=40))
    record = outages(clean_db, _START, _END, now=_END + timedelta(days=1))
    assert record['records'][-1]['until'] == _END.isoformat()


def test_process_starts_inside_the_span(clean_db):
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        for started in (_START - timedelta(days=3), _START + timedelta(hours=5), _START + timedelta(hours=5)):
            cur.execute("INSERT INTO config_generations (fingerprint, pipeline_id, activated_at, reason, "
                        "process_started_at) VALUES ('f', 'stream_a', %s, 'boot', %s)", (started, started))
    record = process_starts(clean_db, _START, _END)
    assert record == {'count': 1, 'days_missing': [], 'starts': [(_START + timedelta(hours=5)).isoformat()]}
