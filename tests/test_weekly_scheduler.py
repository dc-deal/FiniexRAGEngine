"""Weekly scheduler (ISSUE_27) — config→cron mapping, lifecycle, job resilience."""
import asyncio
from datetime import datetime, timezone

from finiexragengine.core.alerts.weekly_scheduler import WeeklyScheduler
from finiexragengine.types.config_types.app_config_types import WeeklyReportConfig

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
