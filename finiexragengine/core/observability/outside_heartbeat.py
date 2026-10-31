"""The outside check's sender — a dead man's switch at an external service.

`/v1/health`, the stall watchdog and the dead-worker check all run inside the process, so they go
silent with the thing they measure: the reset of 2026-09-20 cost 12 h 50 m and was reported by
another project on the bus. A dead machine, a dead process and a frozen event loop have one thing in
common — they stop doing things — so the alarm is raised by the absence of a ping, at a service that
keeps running when this box does not.

Three decisions make the ping mean something:

- **It goes out from this event loop**, next to the work, so a frozen loop stops it too. A scheduled
  task on the box would keep vouching for a frozen process.
- **It goes out only while the engine would answer `ok`**, by the same computation as the route
  (`health_verdict.engine_problems`). A worker that died inside a running process stops the pings
  just as a dead machine does.
- **The first one waits one interval.** A worker that has only just started has nothing to report
  yet, and a restart is not an event worth a ping of its own.

A failed ping costs one log line and never touches the work. The address is a credential, so it
appears in no log line, no error and no status: `httpx` can put the URL into an exception's text,
which is why only the class name of a failure is ever kept (the same rule as `telegram_client`).
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Literal, Optional

import httpx

from finiexragengine.types.config_types.app_config_types import MonitoringConfig

logger = logging.getLogger(__name__)

HeartbeatState = Literal['starting', 'ok', 'withheld', 'failing']

# How long one beat may wait for the verdict, and for the ping. Far below the interval, and nothing
# about either depends on the machine, so it is not a setting.
_TIMEOUT_SECONDS = 10.0


class OutsideHeartbeat:
    """Pings the configured address on an interval while the engine has no problems to report."""

    def __init__(self, config: MonitoringConfig,
                 problems_provider: Callable[[], List[str]],
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self._config = config
        # A provider, not a snapshot, and it may block: the run lock's status can reach the database.
        self._problems_provider = problems_provider
        # One client for the life of the process: a client per ping is a TLS context per ping, the
        # shape that cost this engine its memory slope once already. Injectable for tests.
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
        self._verdict: Optional[asyncio.Task] = None
        self._state: HeartbeatState = 'starting'
        self._beats_sent = 0
        self._beats_withheld = 0
        self._beats_failed = 0
        self._last_sent_at: Optional[datetime] = None
        self._stop = asyncio.Event()

    def status(self) -> Dict[str, object]:
        """What this sender did, for /health. The external service knows only that pings arrived;
        these counters say why they stopped. Never the address, and never the problems' names."""
        return {'state': self._state,
                'interval_seconds': self._config.heartbeat_interval_seconds,
                'beats_sent': self._beats_sent,
                'beats_withheld': self._beats_withheld,
                'beats_failed': self._beats_failed,
                'last_sent_at': self._last_sent_at}

    async def beat(self) -> None:
        """One ping, or the reason there is none. Never raises — the seam the tests drive."""
        problems = await self._problems()
        if problems:
            self._beats_withheld += 1
            self._enter('withheld', ', '.join(problems))
            return
        failure = await self._ping()
        if failure is not None:
            self._beats_failed += 1
            self._enter('failing', failure)
            return
        self._beats_sent += 1
        self._last_sent_at = datetime.now(timezone.utc)
        self._enter('ok', '')

    async def run(self) -> None:
        """Beat on the configured interval until stopped (started by the API lifespan)."""
        self._stop.clear()
        logger.info('[HEARTBEAT] outside check armed — a ping every %ds while the engine is ok, '
                    'the first after one interval', self._config.heartbeat_interval_seconds)
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self._config.heartbeat_interval_seconds)
                return
            except asyncio.TimeoutError:
                await self.beat()

    async def stop(self) -> None:
        """Stop for an orderly shutdown: silence, which the service's grace period absorbs."""
        self._stop.set()
        if self._verdict is not None:
            self._verdict.cancel()

    async def close(self) -> None:
        await self._client.aclose()

    async def _problems(self) -> List[str]:
        """The verdict, off this loop. A verdict that cannot be had is a reason to withhold.

        A hung one is waited for rather than started again: its thread cannot be cancelled, so a new
        one per interval would slowly fill the executor the workers share.
        """
        if self._verdict is None or self._verdict.done():
            self._verdict = asyncio.create_task(asyncio.to_thread(self._problems_provider))
        done, _ = await asyncio.wait({self._verdict}, timeout=_TIMEOUT_SECONDS)
        if not done:
            return ['verdict timed out']
        try:
            return self._verdict.result()
        except Exception as exc:    # noqa: BLE001 — an engine that cannot say is not saying `ok`
            return [f'verdict failed ({type(exc).__name__})']

    async def _ping(self) -> Optional[str]:
        """Send the ping; the failure's token-safe description, or None when it arrived."""
        try:
            response = await self._client.get(self._config.heartbeat_url)
        except Exception as exc:    # noqa: BLE001 — httpx.InvalidURL is no HTTPError; nothing may
            return type(exc).__name__   # reach the work, and str(exc) may carry the address
        return None if response.is_success else f'HTTP {response.status_code}'

    def _enter(self, state: HeartbeatState, detail: str) -> None:
        """Log a change of state, once — a long outage is one line, and the counters keep count."""
        if state == self._state:
            return
        previous, self._state = self._state, state
        if state == 'withheld':
            logger.warning('[HEARTBEAT] ping withheld, the engine is not ok: %s', detail)
        elif state == 'failing':
            logger.warning('[HEARTBEAT] ping failed: %s — the work is not affected', detail)
        else:
            logger.info('[HEARTBEAT] ping delivered (was %s)', previous)
