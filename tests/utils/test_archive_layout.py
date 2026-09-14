"""Archive layout contract (ISSUE_13) — bucket naming, partitioning, range selection.

Pure functions, no I/O: these pin the naming the collector (ISSUE_9) writes and the
TestingIDE reader (#141) selects by. A change here is a change to a shared contract.
"""
from datetime import datetime, timedelta, timezone

import pytest

from finiexragengine.utils.archive_layout import (
    bucket_closes_at,
    bucket_name,
    bucket_path,
    buckets_for_range,
)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_daily_bucket_is_the_utc_date_and_crosses_at_midnight():
    assert bucket_name(_utc(2026, 4, 27, 0, 0), 'daily') == '2026-04-27'
    assert bucket_name(_utc(2026, 4, 27, 23, 59, 59, 999999), 'daily') == '2026-04-27'
    assert bucket_name(_utc(2026, 4, 28, 0, 0), 'daily') == '2026-04-28'


def test_non_utc_moment_buckets_by_its_utc_date():
    # 01:30 +02:00 is 23:30 UTC of the *previous* day — the UTC date owns the bucket.
    cest = timezone(timedelta(hours=2))
    assert bucket_name(datetime(2026, 4, 28, 1, 30, tzinfo=cest), 'daily') == '2026-04-27'


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match='timezone-aware'):
        bucket_name(datetime(2026, 4, 27), 'daily')


def test_weekly_bucket_uses_iso_year_and_zero_padded_week():
    assert bucket_name(_utc(2026, 4, 27), 'weekly') == '2026-W18'   # a Monday
    assert bucket_name(_utc(2026, 5, 3, 23, 59), 'weekly') == '2026-W18'   # its Sunday
    assert bucket_name(_utc(2026, 1, 1), 'weekly') == '2026-W01'
    # The ISO-year edge: 2027-01-01 is a Friday inside 2026's week 53.
    assert bucket_name(_utc(2027, 1, 1), 'weekly') == '2026-W53'


def test_bucket_path_partitions_per_stream():
    path = bucket_path('crypto_sentiment', _utc(2026, 4, 27, 10, 0), 'daily')
    assert str(path) == 'crypto_sentiment/2026-04-27.jsonl'
    variant = bucket_path('crypto_sentiment_4o_enhanced', _utc(2026, 4, 27), 'weekly')
    assert str(variant) == 'crypto_sentiment_4o_enhanced/2026-W18.jsonl'


def test_range_selects_only_overlapping_daily_buckets_in_order():
    names = buckets_for_range(_utc(2026, 4, 28, 6, 0), _utc(2026, 4, 30, 1, 0), 'daily')
    assert names == ['2026-04-28', '2026-04-29', '2026-04-30']


def test_range_inside_one_bucket_is_that_single_bucket():
    assert buckets_for_range(_utc(2026, 4, 27, 1), _utc(2026, 4, 27, 23), 'daily') == [
        '2026-04-27']
    assert buckets_for_range(_utc(2026, 4, 27), _utc(2026, 5, 3), 'weekly') == ['2026-W18']


def test_weekly_range_spans_month_and_year_boundaries():
    assert buckets_for_range(_utc(2026, 12, 28), _utc(2027, 1, 12), 'weekly') == [
        '2026-W53', '2027-W01', '2027-W02']


def test_reversed_range_is_rejected():
    with pytest.raises(ValueError, match='precedes'):
        buckets_for_range(_utc(2026, 4, 28), _utc(2026, 4, 27), 'daily')


# --- when a bucket stops growing (ISSUE_13 follow-up) --------------------------------------------

def test_a_daily_bucket_closes_at_the_next_utc_midnight():
    """The fact the export message was missing. Buckets are UTC and an operator's clock usually is
    not, so "still growing: 2026-08-28" read at 00:59 local — 22:59 UTC — looks exactly like an
    export that has fallen a day behind. It was read that way on 2026-08-28."""
    moment = datetime(2026, 8, 28, 22, 59, tzinfo=timezone.utc)

    assert bucket_name(moment, 'daily') == '2026-08-28'
    assert bucket_closes_at(moment, 'daily') == datetime(2026, 8, 29, tzinfo=timezone.utc)


def test_a_weekly_bucket_closes_on_the_next_monday():
    """ISO weeks start on Monday, so the close is the next Monday 00:00 UTC — not seven days on."""
    friday = datetime(2026, 8, 28, 22, 59, tzinfo=timezone.utc)
    monday = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

    assert bucket_closes_at(friday, 'weekly') == datetime(2026, 8, 31, tzinfo=timezone.utc)
    assert bucket_closes_at(monday, 'weekly') == datetime(2026, 8, 31, tzinfo=timezone.utc)
    assert bucket_name(friday, 'weekly') == bucket_name(monday, 'weekly')


def test_the_close_is_always_after_the_moment_it_is_asked_about():
    """A closing time at or before `now` would mean the current bucket is already closed, which is
    the one thing it cannot be — asserted across a day boundary, a month end and a leap day."""
    for moment in (datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
                   datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc),
                   datetime(2028, 2, 29, 12, 0, tzinfo=timezone.utc)):
        for boundary in ('daily', 'weekly'):
            assert bucket_closes_at(moment, boundary) > moment, (moment, boundary)

