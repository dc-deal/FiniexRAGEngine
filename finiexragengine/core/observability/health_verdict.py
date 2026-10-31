"""What keeps this engine from saying `ok` — one computation for everyone who says it.

`/v1/health` answers with the verdict and the outside check's sender acts on it, and those two must
not be able to disagree: a ping that goes out while the route says `degraded` is the external
service being told something the engine does not believe.

Pure on purpose: the callers gather the three facts, which is where the differences between them
live (the route reads the lock from a worker thread of the server, the sender off its event loop).
"""
from typing import Iterable, List, Optional, Sequence

from finiexragengine.types.worker_types import WorkerState


def engine_problems(states: Sequence[WorkerState], stalled: Iterable[str],
                    lock_held: Optional[bool]) -> List[str]:
    """The named reasons this engine is not healthy; empty means it may say `ok`.

    `lock_held` is None where no claim exists to lose. The names are internal ones and can name a
    private worker, so they go to the log and never onto the public route.
    """
    # 'ok' is a claim, not a default. A worker whose task ended is the strongest reason to
    # withdraw it — everything that worker feeds is frozen until a restart — and a stall is the
    # weaker one. Reported together so an external check sees a single field change, which is
    # the only thing a monitor polls: this endpoint answered 'ok' for the whole 37 hours the
    # crypto ingest worker lay dead on 2026-08-20.
    problems = [state.name for state in states if state.stopped_at is not None] + list(stalled)
    # A producer that has lost its exclusivity while another process holds it is not healthy:
    # two instances may be writing one stream and paying for both. Reporting the field while
    # leaving the verdict at 'ok' would be a field that reads as a measurement and measures
    # nothing — the shape this endpoint was fixed for once already.
    if lock_held is False:
        problems.append('run_lock')
    return problems
