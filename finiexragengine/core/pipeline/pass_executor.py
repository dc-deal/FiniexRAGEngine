"""The thread pool passes run in — deliberately NOT the one that serves the API (2026-09-08).

A pass body is synchronous (feeds, OpenAI, psycopg) and runs in a thread so the event loop keeps
serving. It used `asyncio.to_thread`, which is `run_in_executor(None, …)` — the interpreter's
**default** executor. Starlette runs every `def` endpoint in that same pool, and the diagnostic
surface is full of them: `def health()`, `def report()`, `def catalog()`.

Two properties combine badly there. A pass deadline abandons the *await*, not the thread — a thread
blocked in `getaddrinfo` cannot be cancelled, so it is gone until it returns on its own — and the
worker then starts a fresh pass on the next tick. During a prolonged outage that leaks one
unkillable thread per worker per deadline into the pool that answers "what is wrong".

On 2026-09-08 the engine went silent for 3½ minutes and `/v1/*` returned 502 throughout, cleared
only by restarting the app. That episode was short enough that the mechanism was not proven — but a
diagnostic surface whose availability depends on the thing it diagnoses is the wrong shape either
way, and the fix is one dedicated pool.

The point is not that this pool cannot fill. It is that when it does, **the thing filling it is not
the thing that would tell you.**
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

# Threads per worker: one for the pass in flight, one for a predecessor still stuck behind a
# deadline. Below that a single hung pass would stall its own worker's next tick; far above it the
# pool would just accumulate blocked threads without anyone noticing, which the stall watchdog and
# the host-connectivity event already report properly.
THREADS_PER_WORKER = 2
_FLOOR = 4

T = TypeVar('T')


def pass_pool_size(worker_count: int) -> int:
    """How many threads a fleet of this size gets — its own function so it can be asserted.

    The size is the design decision here, and reading it back off a built pool would mean reaching
    into `ThreadPoolExecutor`'s privates to check it.
    """
    return max(_FLOOR, worker_count * THREADS_PER_WORKER)


def build_pass_executor(worker_count: int) -> ThreadPoolExecutor:
    """One pool for every worker's pass body, named so a thread dump says where it came from."""
    return ThreadPoolExecutor(max_workers=pass_pool_size(worker_count),
                              thread_name_prefix='finiex-pass')


async def run_pass(executor: ThreadPoolExecutor, timeout_seconds: float,
                   fn: Callable[..., T], *args: Any) -> T:
    """Run one pass body under a deadline, off the default executor.

    The deadline abandons the await rather than the thread — a blocked thread cannot be cancelled —
    so the worker resumes on its next tick instead of staying dead until a restart. That trade is
    unchanged; what changed is *which* pool absorbs the abandoned thread.
    """
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(executor, fn, *args),
                                  timeout=timeout_seconds)
