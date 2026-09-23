"""Weekly scheduler (ISSUE_27) — config→cron mapping, lifecycle, job resilience."""
import asyncio
from datetime import datetime, timezone

from finiexragengine.core.alerts.weekly_scheduler import WeeklyScheduler
from finiexragengine.types.config_types.app_config_types import (
    PricingProbeConfig,
    WeeklyReportConfig,
)

_CONFIG = WeeklyReportConfig(enabled=True, day_of_week='sun', hour=18, minute=0,
                             timezone='UTC')


async def _noop() -> None:
    pass


def test_trigger_maps_config_to_the_next_sunday_1800_utc():
    trigger = WeeklyScheduler(_CONFIG, _noop).trigger()
    # 2026-07-20 is a Monday — the next sun/18:00 is the 26th.
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    assert trigger.get_next_fire_time(None, now) == datetime(
        2026, 7, 26, 18, 0, tzinfo=timezone.utc)


def test_start_exposes_next_run_and_stop_clears_it():
    async def scenario() -> None:
        scheduler = WeeklyScheduler(_CONFIG, _noop)
        scheduler.start()
        try:
            assert scheduler.next_run is not None
        finally:
            scheduler.stop()
        assert scheduler.next_run is None

    asyncio.run(scenario())


def test_job_failure_is_caught_and_success_calls_back():
    calls = []

    async def ok() -> None:
        calls.append('sent')

    async def boom() -> None:
        raise RuntimeError('send failed')

    asyncio.run(WeeklyScheduler(_CONFIG, ok)._run())
    assert calls == ['sent']
    asyncio.run(WeeklyScheduler(_CONFIG, boom)._run())   # must not raise


# --- the price probe rides this scheduler (ISSUE_67) --------------------------------------------

_PROBE = PricingProbeConfig(enabled=True, day_of_week='sun', hour=17, minute=30, timezone='UTC')


def test_the_probe_is_a_second_job_with_its_own_cron_and_its_own_id():
    """One APScheduler owner, two jobs. The probe runs BEFORE the report on purpose, so a drift is
    in hand when the report is read rather than arriving after it."""
    async def scenario() -> None:
        scheduler = WeeklyScheduler(_CONFIG, _noop, probe_config=_PROBE, run_probe=_noop)
        scheduler.start()
        try:
            report = scheduler.next_run_of('weekly_report')
            probe = scheduler.next_run_of('price_probe')
            assert report is not None and probe is not None
            assert probe < report                      # 17:30 before 18:00 on the same Sunday
        finally:
            scheduler.stop()

    asyncio.run(scenario())


def test_without_a_probe_configured_the_scheduler_is_exactly_what_it_was():
    async def scenario() -> None:
        scheduler = WeeklyScheduler(_CONFIG, _noop)
        scheduler.start()
        try:
            assert scheduler.next_run_of('weekly_report') is not None
            assert scheduler.next_run_of('price_probe') is None
        finally:
            scheduler.stop()

    asyncio.run(scenario())


def test_a_failing_probe_never_takes_the_weekly_report_with_it():
    """It fetches a page and makes a paid call, so it has more ways to fail than the report does."""
    async def boom() -> None:
        raise RuntimeError('page unreachable')

    asyncio.run(WeeklyScheduler(_CONFIG, _noop, probe_config=_PROBE,
                                run_probe=boom)._run_probe_job())   # must not raise
