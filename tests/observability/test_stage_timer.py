"""Tests for the StageTimer (ISSUE_32) — the shared capture unit, no DB/API."""
import pytest

from finiexragengine.core.observability.stage_timer import StageTimer


def test_time_returns_value_and_records_stage():
    timer = StageTimer()
    assert timer.time('fetch', lambda: 42) == 42
    assert [t.stage for t in timer.timings] == ['fetch']
    timing = timer.timings[0]
    assert timing.duration_ms >= 0.0
    assert timing.ended_at >= timing.started_at


def test_total_ms_sums_all_stages():
    timer = StageTimer()
    timer.time('fetch', lambda: None)
    timer.time('embed', lambda: None)
    assert timer.total_ms() == pytest.approx(
        sum(t.duration_ms for t in timer.timings))
    assert [t.stage for t in timer.timings] == ['fetch', 'embed']


def test_raising_stage_leaves_no_record():
    timer = StageTimer()
    with pytest.raises(ValueError):
        timer.time('fetch', lambda: (_ for _ in ()).throw(ValueError('boom')))
    assert timer.timings == []
