"""Abstract base for a pipeline trigger (interval-pull or event-push)."""
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

# A trigger invokes this callback whenever the pipeline should run.
RunCallback = Callable[[], Awaitable[None]]


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
