"""Weekly report cron (ISSUE_27) — the one APScheduler owner in the process.

Wraps an `AsyncIOScheduler` with a single `CronTrigger` job built from
`WeeklyReportConfig` (validated fields, mapped 1:1 — no raw cron strings). Runs inside
the API process lifespan next to the worker supervisor; the job itself is fully caught:
a failed build/send is a lost message and a log line, never a dead scheduler or app.
ISSUE_55 (floor auto-calibration) will later add its own job to this same unit.
"""
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from finiexragengine.types.config_types.app_config_types import WeeklyReportConfig

logger = logging.getLogger(__name__)

# The scheduler calls back into "build + render + send" — it owns timing, nothing else.
SendCallback = Callable[[], Awaitable[None]]


class WeeklyScheduler:

    def __init__(self, config: WeeklyReportConfig, send_weekly: SendCallback) -> None:
        self._config = config
        self._send_weekly = send_weekly
        self._scheduler: Optional[AsyncIOScheduler] = None

    def start(self) -> None:
        """Schedule the weekly job — must run on a live asyncio loop (API lifespan)."""
        self._scheduler = AsyncIOScheduler(timezone=self._config.timezone)
        self._scheduler.add_job(
            self._run, self.trigger(), id='weekly_report',
            coalesce=True, misfire_grace_time=3600)
        self._scheduler.start()
        logger.info('weekly report scheduled — next run %s', self.next_run)

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def trigger(self) -> CronTrigger:
        """The config→CronTrigger mapping (own seam so the parse is testable)."""
        return CronTrigger(day_of_week=self._config.day_of_week,
                           hour=self._config.hour, minute=self._config.minute,
                           timezone=self._config.timezone)

    @property
    def next_run(self) -> Optional[datetime]:
        if self._scheduler is None:
            return None
        job = self._scheduler.get_job('weekly_report')
        return job.next_run_time if job else None

    async def _run(self) -> None:
        # Never propagate: the report is best-effort, the scheduler must survive it.
        try:
            await self._send_weekly()
        except Exception:
            logger.exception('weekly report job failed')
