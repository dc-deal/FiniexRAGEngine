"""Tests for the weekly resource trend (ISSUE_89) — the DB-free aggregation core.

The line's whole purpose is the **delta against the previous window**: 1,191 MB on 2026-08-01 was
alarming only because there was nothing to compare it to. So the cases that matter are the ones
where a comparison does not exist yet, and the ones where a field is missing.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.resource_report import (
    ResourceStats,
    format_resource_line,
    summarise,
)
from finiexragengine.types.resource_types import ResourceSample

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _samples(values, sockets=24, threads=31):
    return [ResourceSample(ts=_NOW - timedelta(minutes=len(values) - i), rss_mb=float(v),
                           open_sockets=sockets, threads=threads)
            for i, v in enumerate(values)]


def test_min_max_mean_over_the_window():
    stats = summarise(_samples([388.0, 412.0, 447.0]), [])
    assert (stats.samples, stats.rss_min, stats.rss_max) == (3, 388.0, 447.0)
    assert round(stats.rss_mean, 1) == 415.7


def test_sockets_and_threads_report_the_LAST_value_not_a_mean():
    # They are counts of live objects. A mean over a week that contained a restart would describe
    # a process that never existed.
    window = _samples([400.0, 400.0], sockets=10) + _samples([400.0], sockets=42)
    stats = summarise(window, [])
    assert stats.sockets_last == 42


def test_the_delta_against_the_previous_window_is_the_signal():
    stats = summarise(_samples([412.0]), _samples([406.0]))
    assert stats.rss_delta == 6.0
    assert 'vs last week +6 MB rss' in format_resource_line(stats)


def test_a_shrinking_process_renders_a_signed_delta():
    assert 'vs last week -9 MB rss' in format_resource_line(
        summarise(_samples([400.0]), _samples([409.0])))


def test_the_first_week_says_so_instead_of_inventing_a_delta():
    # Without this, week one would render "+412 MB" against a zero that never existed — a jump
    # that would read as exactly the leak the gauge is meant to detect.
    stats = summarise(_samples([412.0]), [])
    assert stats.rss_delta is None
    assert 'first week' in format_resource_line(stats)


def test_an_unsampled_window_says_why_rather_than_rendering_zeroes():
    line = format_resource_line(summarise([], []))
    assert 'not sampled' in line
    assert 'migration 008' in line          # names the three plausible causes


def test_a_refused_socket_count_renders_n_a_not_zero():
    stats = summarise(_samples([412.0], sockets=None), [])
    line = format_resource_line(stats)
    assert 'sockets n/a' in line
    assert 'sockets 0' not in line          # None and zero are different facts


def test_measured_is_false_only_when_nothing_was_sampled():
    assert summarise([], []).measured is False
    assert summarise(_samples([1.0]), []).measured is True


def test_stats_without_a_mean_have_no_delta():
    assert ResourceStats(samples=0, rss_mean_previous=400.0).rss_delta is None
