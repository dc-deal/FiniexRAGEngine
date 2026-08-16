"""Abstract base for a pipeline trigger (interval-pull or event-push)."""
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from finiexragengine.types.trigger_types import TriggerReason

# A trigger invokes this callback whenever the pipeline should run, passing **why** (ISSUE_87).
# The trigger is the only unit that knows: the same `run()` used to be called for the boot pass,
# the scheduled tick and a breaking wake, and the reason was dropped on the floor. No default —
# a caller that says nothing would silently produce the empty value this exists to eliminate.
RunCallback = Callable[[TriggerReason], Awaitable[None]]


class AbstractTrigger(ABC):
    """Contract for what drives a pipeline run.

    interval-pull (now) and event-push (later — e.g. a breaking-news socket,
    ISSUE_6) implement the same start/stop contract; the pipeline does not care
    which one drives it. This mirrors the IDE's SIGNAL vs API/EVENT worker split.
    """

    @abstractmethod
    async def start(self, run: RunCallback) -> None:
        """Begin driving runs; invoke `run` per the trigger's policy."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop driving runs."""
        ...
