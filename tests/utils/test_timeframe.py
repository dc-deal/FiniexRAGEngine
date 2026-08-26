"""Timeframe boundary math (ISSUE_timeframe) — the bar-close grid the eval trigger waits on.

Pure functions, no DB / no API. Pins the alignment for every supported frame, the
strictly-after rule at an exact boundary, and the UTC-only contract.
"""
from datetime import datetime, timedelta, timezone

import pytest

from finiexragengine.utils.timeframe import (
    TIMEFRAMES,
    next_boundary,
    seconds_until_next_boundary,
    supported_timeframes,
    timeframe_minutes,
)


def _utc(y, mo, d, h, mi, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def test_supported_set_is_the_documented_frames():
    assert supported_timeframes() == ['M1', 'M5', 'M10', 'M15', 'M30', 'H1', 'H4', 'D1']
    assert TIMEFRAMES['M10'] == 10 and TIMEFRAMES['H4'] == 240 and TIMEFRAMES['D1'] == 1440


def test_timeframe_minutes_unknown_raises():
    with pytest.raises(ValueError, match='unknown timeframe'):
        timeframe_minutes('M7')


@pytest.mark.parametrize('frame,now,expected', [
    # mid-bar rounds up to the next close on the grid
    ('M10', _utc(2026, 7, 21, 10, 4, 23), _utc(2026, 7, 21, 10, 10)),
    ('M5', _utc(2026, 7, 21, 10, 4, 59), _utc(2026, 7, 21, 10, 5)),
    ('M15', _utc(2026, 7, 21, 10, 14, 0), _utc(2026, 7, 21, 10, 15)),
    ('M30', _utc(2026, 7, 21, 10, 1, 0), _utc(2026, 7, 21, 10, 30)),
    ('H1', _utc(2026, 7, 21, 10, 30, 0), _utc(2026, 7, 21, 11, 0)),
    ('H4', _utc(2026, 7, 21, 10, 30, 0), _utc(2026, 7, 21, 12, 0)),   # grid 00/04/08/12/16/20
    ('D1', _utc(2026, 7, 21, 10, 30, 0), _utc(2026, 7, 22, 0, 0)),    # midnight UTC
])
def test_next_boundary_aligns_to_the_grid(frame, now, expected):
    assert next_boundary(now, frame) == expected


@pytest.mark.parametrize('frame,on_boundary,expected', [
    ('M10', _utc(2026, 7, 21, 10, 10, 0), _utc(2026, 7, 21, 10, 20)),
    ('H4', _utc(2026, 7, 21, 12, 0, 0), _utc(2026, 7, 21, 16, 0)),
    ('D1', _utc(2026, 7, 21, 0, 0, 0), _utc(2026, 7, 22, 0, 0)),
])
def test_exactly_on_a_boundary_advances_to_the_next(frame, on_boundary, expected):
    # The current bar just ran; the trigger must wait for the following close, not fire again now.
    assert next_boundary(on_boundary, frame) == expected


def test_seconds_until_next_boundary_matches_the_gap():
    assert seconds_until_next_boundary(_utc(2026, 7, 21, 10, 4, 0), 'M10') == 360.0
    assert seconds_until_next_boundary(_utc(2026, 7, 21, 10, 0, 0), 'H1') == 3600.0


def test_non_utc_aware_is_normalised():
    # A +02:00 wall time of 12:04 is 10:04 UTC -> next M10 close is 10:10 UTC.
    plus_two = datetime(2026, 7, 21, 12, 4, 0, tzinfo=timezone(timedelta(hours=2)))
    assert next_boundary(plus_two, 'M10') == _utc(2026, 7, 21, 10, 10)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match='naive'):
        next_boundary(datetime(2026, 7, 21, 10, 4, 0), 'M10')
