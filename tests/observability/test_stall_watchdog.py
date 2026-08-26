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


def test_a_silent_worker_is_flagged_then_re_announced():
    """The edge fires once; the standing condition keeps speaking, at a widening interval.

    This test used to assert the opposite — flag once, then silence forever, "the nine-day case".
    That was written against #73's log flooding and it is half right: repeating every tick is
    noise. But going quiet is worse, and it cost 37 hours on 2026-08-20, when `ingest:crypto_news`
    was announced at the 15-minute mark and never mentioned again while it lay dead. The condition
    was in `stalled_workers()` the whole time — nothing said it out loud.
    """
    states = [_ingest(_NOW - timedelta(minutes=20))]
    watchdog = _watchdog(states)

    events = watchdog.check(_NOW)
    assert len(events) == 1
    assert events[0].worker == 'ingest:crypto_news' and events[0].stalled is True
    assert watchdog.stalled_workers() == {'ingest:crypto_news'}

    # Still stalled a tick later — the episode is open, so nothing new to say yet.
    assert watchdog.check(_NOW + timedelta(minutes=5)) == []
    assert watchdog.check(_NOW + timedelta(minutes=30)) == []

    # An hour in, it says so again — and it is still the same open episode, not a new one.
    repeat = watchdog.check(_NOW + timedelta(hours=1, minutes=1))
    assert len(repeat) == 1 and repeat[0].stalled is True
    assert repeat[0].silent_seconds > events[0].silent_seconds     # and getting worse

    # Over nine days it stays legible: a handful of lines a day, not one and not thousands.
    lines = 0
    for minute in range(0, 9 * 24 * 60, 5):                        # the watchdog's own cadence
        lines += len(watchdog.check(_NOW + timedelta(minutes=minute)))
    assert 20 < lines < 60, f'{lines} lines over nine days'
    assert watchdog.stalled_workers() == {'ingest:crypto_news'}


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


# --- ISSUE_89: the resource gauge rides this tick ------------------------------------------

class _RecordingGauge:
    def __init__(self, explode: bool = False) -> None:
        self.calls = 0
        self._explode = explode

    def sample(self):
        self.calls += 1
        if self._explode:
            raise RuntimeError('gauge exploded')
        return object()


def _gauged_watchdog(gauge=None) -> StallWatchdog:
    """A watchdog with no workers — these cases are about the tick, not about stalls."""
    watchdog = StallWatchdog(StallWatchdogConfig(), lambda: [])
    if gauge is not None:
        watchdog.set_gauge(gauge)
    return watchdog


def test_the_tick_samples_the_gauge_when_one_is_attached():
    gauge = _RecordingGauge()
    watchdog = _gauged_watchdog(gauge)
    asyncio.run(watchdog._tick())
    asyncio.run(watchdog._tick())
    # Every tick, not only the ones that produce a stall event — a series with holes in the quiet
    # weeks would be worthless for exactly the question it exists to answer.
    assert gauge.calls == 2


def test_a_watchdog_without_a_gauge_ticks_unchanged():
    asyncio.run(_gauged_watchdog()._tick())    # no attribute error, no behaviour change


def test_a_failing_gauge_does_not_kill_the_tick():
    # `sample()` swallows by contract; this is the second belt. A watchdog that dies because a
    # diagnostic threw is strictly worse than one that misses a sample.
    gauge = _RecordingGauge(explode=True)
    watchdog = _gauged_watchdog(gauge)
    try:
        asyncio.run(watchdog._tick())
    except RuntimeError:
        pass                            # the caller's own try/except is what run() provides
    assert gauge.calls == 1


def test_a_dead_worker_alerts_differently_from_a_stalled_one():
    """'stalled' and 'dead' must not read the same — waiting helps for one and never for the other.

    The 2026-08-20 alert said *"no completed pass for 15m · 3 of 4 workers still running"*, was
    reasonably read as a hiccup that had resolved, and never repeated. The worker was already dead.
    """
    state = _ingest(_NOW - timedelta(hours=20))
    state.stopped_at = _NOW - timedelta(hours=20)
    state.stopped_reason = "NameError: name '_format_age' is not defined"
    watchdog = _watchdog([state])

    event = watchdog.check(_NOW)[0]
    assert event.dead is True
    message = event.message(running=3, total=4)
    assert 'WORKER DIED' in message
    assert 'must be restarted' in message
    assert '_format_age' in message                     # the cause travels with the alert
    assert 'still running' not in message               # no reassurance on a permanent failure


def test_a_restated_stall_says_it_is_a_restatement():
    """Silence after an alert reads as recovery, so a repeat has to announce itself as one."""
    watchdog = _watchdog([_ingest(_NOW - timedelta(minutes=20))])
    first = watchdog.check(_NOW)[0]
    assert first.repeat is False and 'STILL' not in first.message(3, 4)

    again = watchdog.check(_NOW + timedelta(hours=1, minutes=1))[0]
    assert again.repeat is True
    message = again.message(running=3, total=4)
    assert 'STILL stalled' in message and 'Not recovering on its own' in message
    # And the duration — the number that actually grows — leads the detail line.
    assert 'no completed pass for 1h' in message
