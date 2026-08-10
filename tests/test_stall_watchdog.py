"""StallWatchdog — worker liveness detection (ISSUE_75).

The regression these guard: on 2026-08-01 every worker stood still for nine days and no surface
said so. Detection must be edge-triggered (one line per stall, one on recovery) and must not cry
at a merely slow pass — the ingest cadence is 15s, so the floor, not the factor, carries it.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List

from finiexragengine.core.observability.stall_watchdog import StallWatchdog
from finiexragengine.types.config_types.app_config_types import StallWatchdogConfig
from finiexragengine.types.worker_types import WorkerState

_NOW = datetime(2026, 8, 1, 15, 35, 9, tzinfo=timezone.utc)     # the incident's own timestamp


def _ingest(last_run_at, name: str = 'ingest:crypto_news') -> WorkerState:
    state = WorkerState(name=name, kind='ingest', interval_seconds=15)
    state.last_run_at = last_run_at
    return state


def _eval(last_run_at, name: str = 'eval:crypto_sentiment') -> WorkerState:
    state = WorkerState(name=name, kind='eval', interval_seconds=600, timeframe='M10')
    state.last_run_at = last_run_at
    return state


def _watchdog(states: List[WorkerState], **overrides) -> StallWatchdog:
    return StallWatchdog(StallWatchdogConfig(**overrides), lambda: states)


def test_threshold_is_the_floor_for_a_fast_worker():
    # 3 x 15s = 45s would scream at every slow ingest pass — the floor is what makes this usable.
    watchdog = _watchdog([])
    assert watchdog.threshold_seconds(_ingest(_NOW)) == 15 * 60      # floor wins
    assert watchdog.threshold_seconds(_eval(_NOW)) == 3 * 600        # factor wins (30m)


def test_a_slow_pass_is_not_a_stall():
    # 45s of silence on a 15s cadence: past 3x the cadence, nowhere near the floor.
    states = [_ingest(_NOW - timedelta(seconds=45))]
    assert _watchdog(states).check(_NOW) == []


def test_a_silent_worker_is_flagged_once():
    states = [_ingest(_NOW - timedelta(minutes=20))]
    watchdog = _watchdog(states)

    events = watchdog.check(_NOW)
    assert len(events) == 1
    assert events[0].worker == 'ingest:crypto_news' and events[0].stalled is True
    assert watchdog.stalled_workers() == {'ingest:crypto_news'}

    # Still stalled a tick later — and getting worse — but the episode is already open: silence.
    assert watchdog.check(_NOW + timedelta(minutes=5)) == []
    assert watchdog.check(_NOW + timedelta(days=9)) == []          # the nine-day case


def test_a_resumed_worker_reports_exactly_one_recovery():
    state = _ingest(_NOW - timedelta(minutes=20))
    watchdog = _watchdog([state])
    watchdog.check(_NOW)                                # opens the episode

    state.last_run_at = _NOW                            # the worker ran again
    events = watchdog.check(_NOW + timedelta(seconds=5))
    assert len(events) == 1 and events[0].stalled is False
    assert watchdog.stalled_workers() == set()
    assert watchdog.check(_NOW + timedelta(seconds=10)) == []      # no repeat


def test_workers_are_tracked_independently():
    # The incident stalled all four at once, but the point of per-worker tracking is the case
    # where only one dies — it must be nameable while the others keep running.
    healthy, dead = _eval(_NOW), _ingest(_NOW - timedelta(hours=3))
    events = _watchdog([healthy, dead]).check(_NOW)
    assert [event.worker for event in events] == ['ingest:crypto_news']


def test_a_worker_that_never_ran_is_starting_not_stalled():
    assert _watchdog([_ingest(None)]).check(_NOW) == []


def test_disabled_config_detects_nothing():
    states = [_ingest(_NOW - timedelta(days=9))]
    watchdog = _watchdog(states, enabled=False)
    assert watchdog.check(_NOW) == []
    assert watchdog.status()['enabled'] is False


def test_status_reports_the_stalled_set():
    watchdog = _watchdog([_ingest(_NOW - timedelta(minutes=20))])
    watchdog.check(_NOW)
    assert watchdog.status() == {'enabled': True, 'stalled': ['ingest:crypto_news'],
                                 'factor': 3, 'floor_minutes': 15}


def test_the_alert_message_names_the_worker_and_the_silence():
    watchdog = _watchdog([_ingest(_NOW - timedelta(hours=9))])
    event = watchdog.check(_NOW)[0]
    message = event.message(running=3, total=4)
    assert 'ingest:crypto_news' in message
    assert '9h00m' in message
    assert '3 of 4 workers still running' in message


def test_a_tick_delivers_one_alert_then_stays_quiet():
    # The clock is real inside _tick(), so age the state far past any threshold.
    state = _ingest(datetime.now(timezone.utc) - timedelta(days=9))
    sent: List[str] = []

    async def _sink(text: str) -> None:
        sent.append(text)

    watchdog = StallWatchdog(StallWatchdogConfig(), lambda: [state], alert=_sink)

    async def _scenario() -> None:
        await watchdog._tick()
        assert len(sent) == 1 and 'stalled' in sent[0]
        await watchdog._tick()
        assert len(sent) == 1                           # edge-triggered, not per tick
        state.last_run_at = datetime.now(timezone.utc)
        await watchdog._tick()
        assert len(sent) == 2 and 'recovered' in sent[1]

    asyncio.run(_scenario())


def test_a_failing_alert_sink_never_kills_the_watchdog():
    state = _ingest(datetime.now(timezone.utc) - timedelta(days=9))

    async def _broken(text: str) -> None:
        raise RuntimeError('telegram down')

    watchdog = StallWatchdog(StallWatchdogConfig(), lambda: [state], alert=_broken)
    asyncio.run(watchdog._tick())                       # must not raise
    assert watchdog.stalled_workers() == {'ingest:crypto_news'}
