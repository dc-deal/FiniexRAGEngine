"""Interval-pull trigger — runs the pipeline every N seconds."""
from finiexragengine.core.triggers.abstract_trigger import AbstractTrigger, RunCallback


class IntervalTrigger(AbstractTrigger):
    """Fires a pipeline run every `interval_seconds`.

    TODO(impl): background asyncio loop — await run(); await asyncio.sleep(
    interval_seconds); cancellable via stop().
    """

    def __init__(self, interval_seconds: int) -> None:
        self._interval_seconds = interval_seconds
        self._running = False

    async def start(self, run: RunCallback) -> None:
        raise NotImplementedError('IntervalTrigger.start — background run loop')

    async def stop(self) -> None:
        self._running = False
