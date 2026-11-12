"""Signal packages — when runs happen: the weekly rule, special runs, their limits, the quiet minutes.

The rules are the collector's (one contract for both package services); the weekly default here is
Sunday 01:30 UTC, packing to that Sunday's 00:00.
"""
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from finiexragengine.core.packages.package_schedule import ALL, PackageSchedule, ScheduleRefused
from finiexragengine.types.config_types.app_config_types import PackagesConfig
from finiexragengine.utils.quiet_minutes import in_quiet_minutes

_WEDNESDAY = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def schedule(tmp_path: Path) -> PackageSchedule:
    return PackageSchedule(tmp_path, PackagesConfig(enabled=True), lambda: ['stream_a', 'stream_b'])


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_the_weekly_run_is_sunday_0130_utc_strictly_after_now(schedule):
    assert schedule.next_weekly(_WEDNESDAY) == _utc(2026, 7, 26, 1, 30)
    assert schedule.next_weekly(_utc(2026, 7, 26, 1, 30)) == _utc(2026, 8, 2, 1, 30)
    assert schedule.next_weekly(_utc(2026, 7, 26, 1, 29)) == _utc(2026, 7, 26, 1, 30)


def test_a_special_run_defaults_to_its_cutoff_day_at_0100(schedule):
    entry = schedule.add('stream_a', date(2026, 7, 23), None, 'operator', _WEDNESDAY)
    assert (entry['cutoff'], entry['run_at']) == ('2026-07-23T00:00:00+00:00', '2026-07-23T01:00:00+00:00')
    assert schedule.due(_utc(2026, 7, 23, 0, 59)) == []
    assert [e['id'] for e in schedule.due(_utc(2026, 7, 23, 1, 0))] == [entry['id']]


def test_a_cutoff_already_passed_runs_in_five_minutes(schedule):
    entry = schedule.add('stream_a', date(2026, 7, 22), None, 'operator', _WEDNESDAY)
    assert entry['run_at'] == '2026-07-22T09:05:00+00:00'


@pytest.mark.parametrize('source, cutoff, run_at, rule', [
    ('stream_x', date(2026, 7, 23), None, 'no source named'),
    ('stream_a', date(2026, 7, 23), _utc(2026, 7, 23, 0, 20), 'at the earliest'),
    ('stream_a', date(2026, 7, 23), _utc(2026, 7, 23, 23, 58), 'quiet minutes'),
    ('stream_a', date(2026, 7, 25), _utc(2026, 7, 25, 2, 0), 'before the weekly run'),
    ('stream_a', date(2026, 7, 26), _utc(2026, 7, 26, 3, 0), "weekly run's own cutoff"),
])
def test_a_special_run_against_a_rule_is_refused_with_it(schedule, source, cutoff, run_at, rule):
    with pytest.raises(ScheduleRefused, match=rule):
        schedule.add(source, cutoff, run_at, 'operator', _WEDNESDAY)


def test_one_run_per_source_and_cutoff_and_two_per_iso_week(tmp_path):
    schedule = PackageSchedule(tmp_path, PackagesConfig(enabled=True, special_blackout_hours=0),
                               lambda: ['stream_a', 'stream_b'])
    monday = _utc(2026, 7, 27, 9, 0)
    schedule.add('stream_a', date(2026, 7, 28), None, 'operator', monday)
    with pytest.raises(ScheduleRefused, match='already has a run'):
        schedule.add(ALL, date(2026, 7, 28), None, 'operator', monday)       # `all` counts for each
    schedule.add('stream_a', date(2026, 7, 29), None, 'operator', monday)
    with pytest.raises(ScheduleRefused, match='ISO week'):
        schedule.add('stream_a', date(2026, 7, 30), None, 'operator', monday)
    assert schedule.add('stream_b', date(2026, 7, 30), None, 'operator', monday)['state'] == 'planned'


def test_a_cancelled_run_frees_its_slot(schedule):
    entry = schedule.add('stream_a', date(2026, 7, 23), None, 'operator', _WEDNESDAY)
    assert schedule.cancel(entry['id'])['state'] == 'cancelled'
    assert schedule.cancel(entry['id']) is None
    assert schedule.add('stream_a', date(2026, 7, 23), None, 'operator', _WEDNESDAY)['state'] == 'planned'


def test_setting_aside_the_weekly_run_lifts_its_blackout_and_own_cutoff(schedule):
    schedule.set_aside(None, _WEDNESDAY, 'operator')
    assert schedule.weekly_skipped(_utc(2026, 7, 26, 1, 30))
    assert not schedule.weekly_skipped(_utc(2026, 8, 2, 1, 30))
    entry = schedule.add('stream_a', date(2026, 7, 26), _utc(2026, 7, 26, 3, 0), 'operator', _WEDNESDAY)
    assert entry['state'] == 'planned'


def test_set_aside_until_a_date_and_resume(schedule):
    schedule.set_aside(date(2026, 8, 3), _WEDNESDAY, 'operator')
    assert schedule.weekly_skipped(_utc(2026, 7, 26, 1, 30)) and schedule.weekly_skipped(_utc(2026, 8, 2, 1, 30))
    assert not schedule.weekly_skipped(_utc(2026, 8, 9, 1, 30))
    with pytest.raises(ScheduleRefused, match='has passed'):
        schedule.set_aside(date(2026, 7, 1), _WEDNESDAY, 'operator')
    schedule.resume()
    assert not schedule.weekly_skipped(_utc(2026, 7, 26, 1, 30))


def test_the_quiet_minutes_are_one_definition_with_both_ends_included():
    """Two definitions once disagreed at the edge minute (`< 15` and `<= 15`) — there is one now."""
    assert in_quiet_minutes(_utc(2026, 7, 22, 23, 55)) and in_quiet_minutes(_utc(2026, 7, 23, 0, 15))
    assert not in_quiet_minutes(_utc(2026, 7, 22, 23, 54)) and not in_quiet_minutes(_utc(2026, 7, 23, 0, 16))


@pytest.mark.parametrize('fields', [{'hour': 0, 'minute': 10}, {'special_hour': 23, 'special_minute': 59},
                                    {'weekday': 'sunday'}])
def test_the_configuration_refuses_a_time_in_the_quiet_minutes_or_an_unknown_day(fields):
    with pytest.raises(ValidationError):
        PackagesConfig(**fields)
