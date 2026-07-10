"""Stage timer — the shared capture unit of the performance system (ISSUE_32)."""
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, List, TypeVar

from finiexragengine.types.outcome_types import StageTiming

T = TypeVar('T')


class StageTimer:
    """Times each named stage of a pass into a `StageTiming` record.

    The rule (ISSUE_32): every stage of every pass is tracked — cost *and* performance.
    Run a stage through `time('stage', fn)`, get its value back, and the timing lands in
    `timings`: the CLIs echo them via `RunFooter`, ISSUE_7 assembles them into the
    envelope's `RunMetadata`. A stage that raises leaves no record — the exception
    propagates untouched (failed-call durations are a later concern).
    """

    def __init__(self) -> None:
        self._timings: List[StageTiming] = []

    @property
    def timings(self) -> List[StageTiming]:
        return self._timings

    def total_ms(self) -> float:
        return sum(timing.duration_ms for timing in self._timings)

    def time(self, stage: str, fn: Callable[[], T]) -> T:
        """Run `fn`, record its wall-clock duration under `stage`, return its value."""
        started = datetime.now(timezone.utc)
        start = perf_counter()
        value = fn()
        duration_ms = (perf_counter() - start) * 1000.0
        self._timings.append(StageTiming(stage=stage, started_at=started,
                                         ended_at=datetime.now(timezone.utc),
                                         duration_ms=duration_ms))
        return value
