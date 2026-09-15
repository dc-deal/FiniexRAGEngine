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

from finiexragengine.types.config_types.app_config_types import (
    PricingProbeConfig,
    WeeklyReportConfig,
)

logger = logging.getLogger(__name__)

# The scheduler calls back into "build + render + send" — it owns timing, nothing else.
SendCallback = Callable[[], Awaitable[None]]


class WeeklyScheduler:

    def __init__(self, config: WeeklyReportConfig, send_weekly: SendCallback,
                 probe_config: Optional[PricingProbeConfig] = None,
                 run_probe: Optional[SendCallback] = None) -> None:
        self._config = config
        self._send_weekly = send_weekly
        # The price-drift guard (ISSUE_67) rides here rather than bringing a second scheduler: this
        # unit is the one APScheduler owner in the process, and its own note always said further
        # jobs belong in it. Both halves are optional — an engine without the probe configured keeps
        # exactly the scheduler it had.
        self._probe_config = probe_config
        self._run_probe = run_probe
        self._scheduler: Optional[AsyncIOScheduler] = None

    def start(self) -> None:
        """Schedule the weekly job — must run on a live asyncio loop (API lifespan)."""
        self._scheduler = AsyncIOScheduler(timezone=self._config.timezone)
        self._scheduler.add_job(
            self._run, self.trigger(), id='weekly_report',
            coalesce=True, misfire_grace_time=3600)
        self._scheduler.start()
        logger.info('weekly report scheduled — next run %s', self.next_run)
        if self._probe_config is not None and self._run_probe is not None:
            self._scheduler.add_job(
                self._run_probe_job, self.probe_trigger(), id='price_probe',
                coalesce=True, misfire_grace_time=3600)
            logger.info('price probe scheduled — next run %s', self.next_run_of('price_probe'))

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def trigger(self) -> CronTrigger:
        """The config→CronTrigger mapping (own seam so the parse is testable)."""
        return CronTrigger(day_of_week=self._config.day_of_week,
                           hour=self._config.hour, minute=self._config.minute,
                           timezone=self._config.timezone)

    def probe_trigger(self) -> CronTrigger:
        """The probe's own cron mapping — deliberately its own fields, not the report's.

        It runs BEFORE the weekly report by default, so a drift is already in hand when the report
        is read rather than arriving after it.
        """
        config = self._probe_config
        return CronTrigger(day_of_week=config.day_of_week, hour=config.hour,
                           minute=config.minute, timezone=config.timezone)

    def next_run_of(self, job_id: str) -> Optional[datetime]:
        if self._scheduler is None:
            return None
        job = self._scheduler.get_job(job_id)
        return job.next_run_time if job else None

    @property
    def next_run(self) -> Optional[datetime]:
        return self.next_run_of('weekly_report')

    async def _run(self) -> None:
        # Never propagate: the report is best-effort, the scheduler must survive it.
        try:
            await self._send_weekly()
        except Exception:
            logger.exception('weekly report job failed')

    async def _run_probe_job(self) -> None:
        # Same rule, and it matters more here: the probe makes a paid call and touches the network,
        # so it has more ways to fail — and a failure must cost one log line, never the weekly
        # report that shares this scheduler.
        try:
            await self._run_probe()
        except Exception:
            logger.exception('price probe job failed')
