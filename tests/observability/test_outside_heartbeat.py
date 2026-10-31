"""OutsideHeartbeat — the dead man's switch sender.

The regressions these guard: a ping that vouches for an engine `/v1/health` calls degraded, a failed
ping that reaches the work, and the address — a credential — turning up in a log line, an error or a
status.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List

import httpx
import pytest

from finiexragengine.core.observability import outside_heartbeat
from finiexragengine.core.observability.health_verdict import engine_problems
from finiexragengine.core.observability.outside_heartbeat import OutsideHeartbeat
from finiexragengine.types.config_types.app_config_types import MonitoringConfig
from finiexragengine.types.worker_types import WorkerState

_URL = 'https://ping.example/0123-secret-address'


def _config(**overrides) -> MonitoringConfig:
    return MonitoringConfig(heartbeat_url=_URL, **overrides)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _counting(calls: List[str], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status)
    return handler


def _worker(name: str = 'eval:a_pipeline', dead: bool = False) -> WorkerState:
    state = WorkerState(name=name, kind='eval', interval_seconds=600)
    if dead:
        state.stopped_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    return state


# --- the verdict -----------------------------------------------------------------------------------

def test_a_healthy_engine_has_no_problems():
    assert engine_problems([_worker()], [], True) == []
    assert engine_problems([], [], None) == []      # no workers and no claim to lose: nothing wrong


@pytest.mark.parametrize('states, stalled, lock_held, expected', [
    ([_worker('eval:dead', dead=True)], [], True, ['eval:dead']),
    ([_worker()], ['ingest:silent'], True, ['ingest:silent']),
    ([_worker()], [], False, ['run_lock']),
    ([_worker('eval:dead', dead=True)], ['ingest:silent'], False,
     ['eval:dead', 'ingest:silent', 'run_lock']),
])
def test_each_of_the_three_reasons_is_named(states, stalled, lock_held, expected):
    assert engine_problems(states, stalled, lock_held) == expected


# --- the ping --------------------------------------------------------------------------------------

def test_a_healthy_engine_pings_and_counts_it():
    calls: List[str] = []
    heartbeat = OutsideHeartbeat(_config(), lambda: [], _client(_counting(calls)))
    assert heartbeat.status()['state'] == 'starting'
    asyncio.run(heartbeat.beat())
    status = heartbeat.status()
    assert calls == [_URL]
    assert (status['state'], status['beats_sent'], status['beats_withheld'], status['beats_failed']) == (
        'ok', 1, 0, 0)
    assert status['last_sent_at'] is not None


def test_an_unhealthy_engine_sends_nothing_and_says_why_only_in_the_log(caplog):
    calls: List[str] = []
    heartbeat = OutsideHeartbeat(_config(), lambda: ['eval:a_pipeline'], _client(_counting(calls)))
    with caplog.at_level(logging.WARNING):
        asyncio.run(heartbeat.beat())
    status = heartbeat.status()
    assert calls == []
    assert (status['state'], status['beats_sent'], status['beats_withheld']) == ('withheld', 0, 1)
    assert 'eval:a_pipeline' in caplog.text
    assert 'eval:a_pipeline' not in str(status)      # the status serves a public route


def test_a_verdict_that_raises_withholds():
    def broken() -> List[str]:
        raise RuntimeError('the database is gone')
    calls: List[str] = []
    heartbeat = OutsideHeartbeat(_config(), broken, _client(_counting(calls)))
    asyncio.run(heartbeat.beat())
    assert calls == [] and heartbeat.status()['state'] == 'withheld'


def test_a_hung_verdict_withholds_and_does_not_pile_up_threads(monkeypatch):
    monkeypatch.setattr(outside_heartbeat, '_TIMEOUT_SECONDS', 0.01)
    started: List[float] = []

    def hung() -> List[str]:
        started.append(time.monotonic())
        time.sleep(0.2)
        return []

    async def scenario(heartbeat: OutsideHeartbeat) -> None:
        await heartbeat.beat()
        await heartbeat.beat()      # the first thread is still in there: wait for it, start none
        await asyncio.sleep(0.25)   # let it end before the loop closes

    calls: List[str] = []
    heartbeat = OutsideHeartbeat(_config(), hung, _client(_counting(calls)))
    asyncio.run(scenario(heartbeat))
    assert calls == [] and heartbeat.status()['beats_withheld'] == 2
    assert len(started) == 1


@pytest.mark.parametrize('handler', [
    lambda request: httpx.Response(500),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError(f'cannot reach {_URL}')),
    lambda request: (_ for _ in ()).throw(httpx.ReadTimeout(f'timed out on {_URL}')),
    lambda request: (_ for _ in ()).throw(httpx.InvalidURL(f'bad {_URL}')),   # not an HTTPError
])
def test_a_failed_ping_counts_logs_once_and_never_shows_the_address(caplog, handler):
    heartbeat = OutsideHeartbeat(_config(), lambda: [], _client(handler))

    async def three() -> None:
        for _ in range(3):
            await heartbeat.beat()

    with caplog.at_level(logging.DEBUG, logger='finiexragengine'):
        asyncio.run(three())
    assert heartbeat.status()['beats_failed'] == 3
    assert heartbeat.status()['state'] == 'failing'
    assert [record.levelname for record in caplog.records
            if '[HEARTBEAT]' in record.getMessage()] == ['WARNING']   # a change of state, once
    assert _URL not in caplog.text and 'secret-address' not in caplog.text
    assert 'secret-address' not in str(heartbeat.status())


def test_recovery_logs_once_and_the_counters_keep_the_history(caplog):
    problems: List[str] = ['eval:a_pipeline']
    calls: List[str] = []
    heartbeat = OutsideHeartbeat(_config(), lambda: list(problems), _client(_counting(calls)))

    async def scenario() -> None:
        await heartbeat.beat()
        problems.clear()
        await heartbeat.beat()
        await heartbeat.beat()

    with caplog.at_level(logging.INFO, logger='finiexragengine'):
        asyncio.run(scenario())
    status = heartbeat.status()
    assert (status['beats_withheld'], status['beats_sent'], status['state']) == (1, 2, 'ok')
    assert sum('delivered' in record.getMessage() for record in caplog.records) == 1


# --- the loop --------------------------------------------------------------------------------------

def _fast() -> MonitoringConfig:
    # Below the validated minimum on purpose: the loop's behaviour, not the production cadence.
    return MonitoringConfig.model_construct(heartbeat_url=_URL, heartbeat_interval_seconds=0.02)


def test_the_first_beat_waits_one_interval():
    calls: List[str] = []
    heartbeat = OutsideHeartbeat(_fast(), lambda: [], _client(_counting(calls)))

    async def scenario() -> None:
        task = asyncio.create_task(heartbeat.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls == []          # started and armed, nothing sent yet
        await asyncio.sleep(0.1)
        await heartbeat.stop()
        await task

    asyncio.run(scenario())
    assert len(calls) >= 1


def test_a_failing_path_does_not_end_the_loop_and_stop_does():
    attempts: List[int] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError('no route')

    heartbeat = OutsideHeartbeat(_fast(), lambda: [], _client(refuse))

    async def scenario() -> None:
        task = asyncio.create_task(heartbeat.run())
        await asyncio.sleep(0.15)
        assert not task.done()
        await heartbeat.stop()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())
    assert len(attempts) >= 2
