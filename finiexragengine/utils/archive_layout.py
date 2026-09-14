"""Output-archive layout — the naming contract for the rotated JSONL archive (ISSUE_13).

The engine owns the *layout*; the durable archive itself is written downstream by the
collector (ISSUE_9) and read by the TestingIDE (their #141). These pure functions are the
reference implementation all three sides share: the mock generator emits with them, the
tests pin them, the collector mirrors them. Locked before live collection starts — a
populated archive cannot be re-bucketed cheaply.

Contract in three sentences: an envelope lands in the bucket of its **`collected_msc`**
(collection time, UTC — consistent with the no-look-ahead merge model); a stream
(`pipeline_id`, incl. `_variant` fan-out streams) partitions into its own directory with
one boundary kept for its whole history; a closed bucket is immutable, and a range read
loads exactly the buckets overlapping the range, concatenated in order.
"""
from datetime import datetime, timedelta, timedelta, timezone
from pathlib import PurePosixPath
from typing import List, Literal

Boundary = Literal['daily', 'weekly']


def bucket_name(moment: datetime, boundary: Boundary) -> str:
    """The (sortable) bucket stem for a collection moment — no extension, no stream.

    Daily: the UTC calendar date, `2026-04-27`. Weekly: the ISO week, `2026-W18`
    (ISO year + zero-padded ISO week, Monday start) — note the ISO-year edge:
    2027-01-01 buckets as `2026-W53`, keeping every week's file name unique and sorted.
    """
    moment = _require_utc(moment)
    if boundary == 'daily':
        return f'{moment:%Y-%m-%d}'
    iso = moment.isocalendar()
    return f'{iso.year}-W{iso.week:02d}'


def bucket_closes_at(moment: datetime, boundary: Boundary) -> datetime:
    """The instant the bucket containing `moment` stops growing — the start of the next one.

    Here rather than in the exporter because it is the same fact as `bucket_name`: what a bucket IS.
    An exporter that computed its own closing time would be a second definition of the boundary, and
    the two would agree until someone changed one.

    It exists so a skipped bucket can say **when** it closes rather than only that it is open. That
    is not decoration: buckets are UTC, an operator's clock usually is not, and "still growing:
    2026-08-28" read at 00:59 local (22:59 UTC) looks exactly like an export that has fallen a day
    behind. It cost a round trip on 2026-08-28.
    """
    moment = _require_utc(moment)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if boundary == 'daily':
        return midnight + timedelta(days=1)
    # Weekly buckets are ISO weeks, Monday start — so the close is the next Monday 00:00 UTC.
    return midnight + timedelta(days=7 - moment.isoweekday() + 1)


def bucket_path(stream_id: str, moment: datetime, boundary: Boundary) -> PurePosixPath:
    """Relative archive path: `<stream_id>/<bucket>.jsonl` — one directory per stream."""
    return PurePosixPath(stream_id) / f'{bucket_name(moment, boundary)}.jsonl'


def buckets_for_range(start: datetime, end: datetime,
                      boundary: Boundary) -> List[str]:
    """All bucket stems overlapping `[start, end]` (inclusive), in chronological order.

    The reader contract (#141): for a query range, load exactly these buckets and
    concatenate them in this order — never the whole archive, never out of order.
    """
    start, end = _require_utc(start), _require_utc(end)
    if end < start:
        raise ValueError('range end precedes start')
    step = timedelta(days=1 if boundary == 'daily' else 7)
    names: List[str] = []
    cursor = start
    while True:
        name = bucket_name(cursor, boundary)
        if not names or names[-1] != name:
            names.append(name)
        if bucket_name(end, boundary) == name:
            return names
        cursor += step


def _require_utc(moment: datetime) -> datetime:
    # Buckets are UTC by contract — a naive datetime would bucket by machine locale,
    # and a +02:00 moment must land in its *UTC* date's bucket, so convert.
    if moment.tzinfo is None:
        raise ValueError('archive bucketing needs a timezone-aware datetime (UTC contract)')
    return moment.astimezone(timezone.utc)
