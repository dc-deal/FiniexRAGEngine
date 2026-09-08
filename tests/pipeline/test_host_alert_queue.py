"""The connectivity alert survives the outage it reports (2026-09-08).

The alert channel travels over the network the alert is *about*. On 2026-09-08 two opening alarms
were lost that way while their recovery five minutes later went through, so the operator's inbox
held a lone "host connectivity recovered after 5m" with no preceding alarm — which reads as noise
and is worse than silence.

Everything here drives the real worker pass (`IngestResult.host_event` → `_report_host_event` →
`_deliver`); nothing pokes at the queue directly, because the property under test is what the
operator's inbox ends up holding.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from finiexragengine.core.pipeline import ingest_worker as worker_module
from finiexragengine.core.pipeline.ingest_worker import IngestWorker
from finiexragengine.core.triggers.interval_trigger import IntervalTrigger
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import HostEvent, IngestResult

_SET = SourceSetConfig(
    source_set_id='forex_news',
    sources=[{'source_id': 's1', 'url': 'https://example.test'}])

_T0 = datetime(2026, 9, 8, 9, 0, 0, tzinfo=timezone.utc)


class _Clock:
    """A controllable stand-in for `datetime` inside the worker module.

    The delay prefix is a difference between two wall-clock instants, so proving it means owning
    the clock — a test that slept for a minute would assert the same thing far more slowly.
    """
    current = _T0

    @classmethod
    def now(cls, tz: Optional[timezone] = None) -> datetime:
        return cls.current

    @classmethod
    def advance(cls, seconds: float) -> None:
        cls.current = cls.current + timedelta(seconds=seconds)


def _opened(failed: int = 11) -> HostEvent:
    return HostEvent(source_set='forex_news', failed=failed, pollable=11, started_at=_T0,
                     backoff_until=_T0 + timedelta(minutes=5),
                     fleet=f'forex_news {failed}/11', opened=True)


def _resumed(after: float = 300.0) -> HostEvent:
    return HostEvent(source_set='forex_news', failed=0, pollable=11, started_at=_T0,
                     backoff_until=_T0, resumed=True, duration_seconds=after)


class _ScriptedIngestor:
    """Returns a prepared result per pass, then quiet ones — and moves the clock each pass."""
    def __init__(self, events: List[Optional[HostEvent]], seconds_per_pass: float = 300.0):
        self._events = list(events)
        self._seconds = seconds_per_pass
        self.runs = 0

    @property
    def remaining(self) -> int:
        return len(self._events)

    def run(self) -> IngestResult:
        self.runs += 1
        _Clock.advance(self._seconds)
        event = self._events.pop(0) if self._events else None
        return IngestResult(fetched=1, host_event=event)


class _Channel:
    """An alert channel that can be down — the 2026-09-08 shape, not an abstract failure."""
    def __init__(self, fail_while: Optional[List[bool]] = None):
        self.sent: List[str] = []
        self.attempts = 0
        self._script = list(fail_while or [])

    def down_after(self, count: int) -> None:
        self._script = [False] * count + [True] * 50

    async def __call__(self, message: str) -> None:
        self.attempts += 1
        failing = self._script.pop(0) if self._script else False
        if failing:
            raise ConnectionError('getaddrinfo failed')
        self.sent.append(message)


def _drive(events: List[Optional[HostEvent]], channel: _Channel,
           seconds_per_pass: float = 300.0) -> _ScriptedIngestor:
    """Run the worker until every scripted result has been through a pass."""
    _Clock.current = _T0
    ingestor = _ScriptedIngestor(events, seconds_per_pass)

    async def _scenario() -> None:
        worker = IngestWorker(_SET, ingestor, IntervalTrigger(0.005), 30,
                              on_host_event=channel)
        task = asyncio.create_task(worker.start())
        deadline = asyncio.get_running_loop().time() + 3.0
        while ingestor.remaining and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        await worker.stop()
        await task

    asyncio.run(_scenario())
    return ingestor


def _ratio(line: str) -> str:
    """The identifying part of an alert line, as the operator would scan for it."""
    if 'recovered' in line:
        return 'recovered'
    return line.split(' unreachable')[0].rsplit(' ', 1)[-1]


def _with_clock(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, 'datetime', _Clock)


# --- the defect that is visible in the operator's inbox right now ------------------------------

def test_a_recovery_never_goes_out_alone_while_its_alarm_is_still_undelivered(monkeypatch):
    """The whole point. Two lines arrive, the alarm first — an incident, not a stray all-clear."""
    _with_clock(monkeypatch)
    channel = _Channel(fail_while=[True])          # the opening alarm is lost to the outage
    _drive([_opened(), _resumed()], channel)

    assert len(channel.sent) == 2
    assert 'host connectivity — forex_news 11/11' in channel.sent[0]
    assert 'recovered after 5m' in channel.sent[1]


def test_the_delayed_alarm_says_how_late_it_is(monkeypatch):
    """Without it, an alarm arriving after its own all-clear looks like a second outage."""
    _with_clock(monkeypatch)
    channel = _Channel(fail_while=[True])
    _drive([_opened(), _resumed()], channel)

    assert channel.sent[0].startswith('[delayed 5m] ')
    assert not channel.sent[1].startswith('[delayed')   # the one that went out on time


def test_a_prompt_retry_carries_no_delay_prefix(monkeypatch):
    """Under a minute the prefix would be noise — the message was effectively on time."""
    _with_clock(monkeypatch)
    channel = _Channel(fail_while=[True])
    _drive([_opened(), _resumed()], channel, seconds_per_pass=10.0)

    assert channel.sent[0].startswith('host connectivity — ')
    assert len(channel.sent) == 2


def test_a_failure_mid_flush_re_queues_from_that_point_and_nothing_is_sent_twice(monkeypatch):
    """The flush is not atomic, so the half that got through must not be replayed.

    Two alarms are queued; the channel comes back for the first and drops out again on the second.
    The delivered one leaves the queue, the rest are held **from the failing message onward**, and
    the eventual inbox holds each line exactly once, in the order the incident happened.
    """
    _with_clock(monkeypatch)
    channel = _Channel()
    # alarm 9/11 lost · flush delivers it, fails on 10/11 · everything after that gets through.
    channel._script = [True, False, True] + [False] * 20
    _drive([_opened(9), _opened(10), _resumed(), _opened(11)], channel)

    assert len(channel.sent) == len(set(channel.sent)), 'a message was delivered twice'
    assert [_ratio(line) for line in channel.sent] == ['9/11', '10/11', 'recovered', '11/11']


def test_the_queue_is_bounded_and_keeps_the_newest(monkeypatch):
    """A held queue is a courtesy, not a ledger — an all-night outage must not replay itself.

    Eleven alarms are lost against a bound of eight. When the channel returns, the operator gets
    the eight most recent plus the message that triggered the flush: during an incident the recent
    minutes are the useful ones, and the oldest are the ones already superseded.
    """
    _with_clock(monkeypatch)
    channel = _Channel()
    channel._script = [True] * 11 + [False] * 50
    alarms: List[Optional[HostEvent]] = [_opened(failed) for failed in range(1, 12)]
    _drive(alarms + [_resumed()], channel)

    assert len(channel.sent) == worker_module._PENDING_ALERTS + 1
    assert _ratio(channel.sent[0]) == '4/11', 'the newest eight are what survived'
    assert _ratio(channel.sent[-2]) == '11/11'
    assert _ratio(channel.sent[-1]) == 'recovered'
    assert '1/11' not in [_ratio(line) for line in channel.sent], 'the oldest were dropped'


def test_the_queue_drains_on_the_next_alert_and_not_on_a_quiet_pass(monkeypatch):
    """A stated limitation, pinned so it is a decision rather than a surprise.

    Nothing here is a timer: the queue is flushed when the *next* connectivity event is reported.
    An incident whose last message failed therefore waits for the next incident. That is the trade
    the design took (no retry loop, no persistence) — and it holds because the message that opens
    an outage is the one worth keeping, and an outage is always followed by its recovery.
    """
    _with_clock(monkeypatch)
    channel = _Channel()
    channel._script = [True] + [False] * 20
    _drive([_opened(), None, None, None], channel)     # three quiet passes after the lost alarm

    assert channel.sent == [], 'a pass with no host event never touches the queue'


def test_an_alert_channel_that_is_down_never_fails_the_pass(monkeypatch):
    """The worker keeps ingesting through an undeliverable alert — the pass already succeeded."""
    _with_clock(monkeypatch)
    channel = _Channel()
    channel.down_after(0)
    ingestor = _drive([_opened(), None, None], channel)

    assert ingestor.runs >= 3
