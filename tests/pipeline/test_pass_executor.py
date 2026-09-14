"""Passes do not run in the pool that serves the API (2026-09-08).

`asyncio.to_thread` is `run_in_executor(None, …)` — the interpreter's **default** executor, which
Starlette also uses for every sync `def` endpoint, and the diagnostic surface is full of them. A
pass deadline abandons the *await*, not the thread, so a prolonged outage leaks one uncancellable
thread per worker per deadline into the pool that answers "what is wrong". On 2026-09-08 `/v1/*`
returned 502 for 3½ minutes while the engine was silent.

The identity is the whole fix, so it is asserted directly: nothing else in the suite would notice
if a later edit put `to_thread` back.
"""
import asyncio
import threading
import time
from typing import List

from finiexragengine.core.pipeline.ingest_worker import IngestWorker
from finiexragengine.core.pipeline.pass_executor import (
    THREADS_PER_WORKER,
    build_pass_executor,
    pass_pool_size,
    run_pass,
)
from finiexragengine.core.triggers.interval_trigger import IntervalTrigger
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import IngestResult

_SET = SourceSetConfig(
    source_set_id='crypto_news',
    sources=[{'source_id': 's1', 'url': 'https://example.test'}])


def _thread_name() -> str:
    return threading.current_thread().name


# --- the identity ------------------------------------------------------------------------------

def test_a_pass_runs_in_the_dedicated_pool_and_not_the_default_one():
    """The two names must differ — that difference IS the fix."""
    async def _scenario():
        executor = build_pass_executor(2)
        try:
            in_pass = await run_pass(executor, 5, _thread_name)
            in_api = await asyncio.to_thread(_thread_name)     # what a sync endpoint gets
        finally:
            executor.shutdown(wait=False)
        return in_pass, in_api

    in_pass, in_api = asyncio.run(_scenario())
    assert in_pass.startswith('finiex-pass'), in_pass
    assert not in_api.startswith('finiex-pass'), 'the pass pool is serving API work'


def test_a_worker_built_without_a_pool_still_gets_one_of_its_own():
    """The isolation must not depend on the supervisor wiring it — the CLI paths build workers too."""
    seen: List[str] = []

    class _NamingIngestor:
        def run(self) -> IngestResult:
            seen.append(_thread_name())
            return IngestResult(fetched=1)

    async def _scenario():
        worker = IngestWorker(_SET, _NamingIngestor(), IntervalTrigger(0.005), 30)
        task = asyncio.create_task(worker.start())
        deadline = asyncio.get_running_loop().time() + 2.0
        while not seen and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        await worker.stop()
        await task

    asyncio.run(_scenario())
    assert seen and seen[0].startswith('finiex-pass'), seen


def test_a_full_pass_pool_still_leaves_the_api_answering():
    """The property the identity buys, stated as behaviour rather than as a thread name.

    Every slot of the pass pool is held by a blocked pass — the outage shape, uncancellable — and a
    sync endpoint's work must still be scheduled and returned promptly.
    """
    executor = build_pass_executor(1)             # the floor: 4 slots
    released = threading.Event()

    def _blocked() -> str:
        released.wait(timeout=5.0)
        return 'done'

    async def _scenario() -> float:
        stuck = [asyncio.create_task(run_pass(executor, 5, _blocked))
                 for _ in range(pass_pool_size(1))]
        await asyncio.sleep(0.05)                 # every slot taken
        started = time.monotonic()
        assert await asyncio.to_thread(lambda: 'api') == 'api'
        elapsed = time.monotonic() - started
        released.set()
        await asyncio.gather(*stuck)
        return elapsed

    elapsed = asyncio.run(_scenario())
    executor.shutdown(wait=False)
    assert elapsed < 1.0, f'the API call waited {elapsed:.2f}s behind the blocked passes'


# --- sizing ------------------------------------------------------------------------------------

def test_the_pool_holds_a_stuck_predecessor_next_to_every_pass_in_flight():
    """Two threads per worker, so one abandoned pass never blocks its worker's next tick."""
    assert pass_pool_size(6) == 6 * THREADS_PER_WORKER


def test_a_small_fleet_still_gets_a_floor():
    """One worker must not mean a one-slot pool: the abandoned pass would take the next tick."""
    assert pass_pool_size(1) >= 2 * THREADS_PER_WORKER
    assert pass_pool_size(0) > 0                          # a fleet built before its workers are


# --- the deadline, unchanged -------------------------------------------------------------------

def test_the_deadline_still_abandons_the_await_and_the_thread_keeps_running():
    """Moving pools changed which pool absorbs the abandoned thread, never the trade itself."""
    executor = build_pass_executor(1)
    finished = threading.Event()

    def _slow() -> str:
        time.sleep(0.15)
        finished.set()
        return 'late'

    async def _scenario() -> None:
        try:
            await run_pass(executor, 0.02, _slow)
            raise AssertionError('the deadline did not fire')
        except asyncio.TimeoutError:
            pass

    asyncio.run(_scenario())
    assert finished.wait(timeout=2.0), 'the abandoned thread must still be running, uncancelled'
    executor.shutdown(wait=False)
