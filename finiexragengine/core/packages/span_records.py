"""When the engine started, and when it was not running, inside a package's span.

The manifest carries both beside the members (`process_starts`, `outages`), because a gap in a day
file is ambiguous on its own: no pass ran, or nothing was there to pass on. The consumer keeps the
manifest beside the zip as part of the record.

- **Starts** come from `config_generations.process_started_at`: every process that attached its
  runners logs one activation per stream, and the table is kept forever. Days before its first row
  are `days_missing` — the record does not reach them, which is different from "no start".
- **Outages** are the gaps in `resource_samples`, the watchdog's 60 s sample, longer than
  `OUTAGE_GAP_SECONDS`. That table is kept 14 days, so an older span is mostly `days_missing` and
  says so instead of claiming the engine never stopped.

`None` for either means "not known" (the table is absent) and is never written as an empty list —
the contract the collector's manifests set: an object means recorded.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg

# Three missed 60 s samples: longer than any garbage-collection pause or slow tick measured, shorter
# than a restart (stop, drain, start is minutes).
OUTAGE_GAP_SECONDS = 180


def process_starts(database_url: str, start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
    """`{count, days_missing, starts}` for the process starts in `[start, end)`, or None (no table)."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        if not _table_exists(cur, 'config_generations'):
            return None
        cur.execute('SELECT min(process_started_at) FROM config_generations')
        first = cur.fetchone()[0]
        cur.execute('SELECT DISTINCT process_started_at FROM config_generations '
                    'WHERE process_started_at >= %s AND process_started_at < %s ORDER BY 1',
                    (start, end))
        starts = [row[0] for row in cur.fetchall()]
    return {'count': len(starts), 'days_missing': _days_not_reached(start, end, first),
            'starts': [_iso(moment) for moment in starts]}


def outages(database_url: str, start: datetime, end: datetime, now: datetime) -> Optional[Dict[str, Any]]:
    """`{count, days_missing, records}` for the engine's outages in `[start, end)`, or None (no table).

    A record is `{from, until, seconds}`. The edges count: a span that begins inside an outage, or
    one still going at the span's end (or now, whichever is earlier), is reported up to that edge.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        if not _table_exists(cur, 'resource_samples'):
            return None
        cur.execute('SELECT min(ts) FROM resource_samples')
        oldest = cur.fetchone()[0]
        days_missing = _days_not_reached(start, end, oldest)
        if oldest is None:
            return {'count': 0, 'days_missing': days_missing, 'records': []}
        covered_from = max(start, oldest)
        horizon = min(end, now)
        gap = timedelta(seconds=OUTAGE_GAP_SECONDS)
        cur.execute('SELECT ts FROM resource_samples WHERE ts >= %s AND ts < %s ORDER BY ts',
                    (covered_from - gap, horizon + gap))
        samples = [row[0] for row in cur.fetchall()]
    records: List[Dict[str, Any]] = []
    if covered_from < horizon:
        # The edges first: before the first sample in reach, and after the last one.
        points = [covered_from] + samples + [horizon]
        for before, after in zip(points, points[1:]):
            if after - before > gap:
                begin, finish = max(before, covered_from), min(after, horizon)
                if finish > begin:
                    records.append({'from': _iso(begin), 'until': _iso(finish),
                                    'seconds': int((finish - begin).total_seconds())})
    return {'count': len(records), 'days_missing': days_missing, 'records': records}


def _days_not_reached(start: datetime, end: datetime, first_record: Optional[datetime]) -> List[str]:
    """The UTC days of `[start, end)` that end before the record begins — all of them without one."""
    days: List[str] = []
    cursor = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < end:
        day_end = cursor + timedelta(days=1)
        if first_record is None or day_end <= first_record:
            days.append(f'{cursor:%Y-%m-%d}')
        cursor = day_end
    return days


def _table_exists(cur: psycopg.Cursor, table: str) -> bool:
    cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s', (table,))
    return cur.fetchone()[0] > 0


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()
