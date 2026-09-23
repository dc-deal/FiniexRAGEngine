"""One fetched reading, wearing the shape the renderer already reads (ISSUE_126 Phase 2).

`LiveDisplay` asks its collaborators for very little — seven accessors on `EngineStats`, plus
`status()`, `stalled_workers()`, `over_ceiling`/`latest()` and a states callable. That narrowness is
what makes a viewer possible without touching a line of rendering code: this module hands back
objects answering exactly those names, rebuilt from a payload the engine serialized.

**The engine's verdicts are passed through, never recomputed.** `over_ceiling` was decided against
`diagnostics.resource_rss_warn_mb` on the other machine and `stalled` against each worker's own
cadence. Deriving either here from a local config is the defect the FiniexDataCollector paid for,
when their renderer read a *local* file-size limit and drew a remote file of 12,737 ticks as 1274 %
of a boundary that instance does not have.

**And an absence is carried, not smoothed.** Where the payload says a collaborator was not
established, the corresponding accessor below is `None` — which the renderer draws as *unknown*
rather than as healthy. That distinction is the whole reason the payload separates `null` from `[]`.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from finiexragengine.core.ui.dashboard_snapshot import DashboardState, ResourceReading
from finiexragengine.core.ui.engine_stats import (
    BreakingRecord,
    BreakingSnapshot,
    IngestSnapshot,
    LlmSnapshot,
    RetrievalSnapshot,
    SourcesSnapshot,
    StreamEvent,
)
from finiexragengine.types.worker_types import WorkerState
from finiexragengine.utils.dataclass_json import from_jsonable


class _RemoteBudget:
    """Answers `status()` with what the engine's own guard said."""

    def __init__(self, status: Dict[str, Any]) -> None:
        self._status = status

    def status(self) -> Dict[str, Any]:
        return self._status


class _RemoteWatchdog:
    """Answers `stalled_workers()` with the engine's verdict, never a re-derivation."""

    def __init__(self, stalled: List[str]) -> None:
        self._stalled = set(stalled)

    def stalled_workers(self) -> Set[str]:
        return self._stalled


class _RemoteGauge:
    """Answers `over_ceiling` and `latest()` — the verdict the engine reached, and its sample."""

    def __init__(self, reading: ResourceReading) -> None:
        self._reading = reading

    @property
    def over_ceiling(self) -> bool:
        return self._reading.over_ceiling

    def latest(self) -> Optional[ResourceReading]:
        # `rss_mb` is the only field the header reads off it, and `ResourceReading` carries it under
        # that name — so the gauge's own sample type is not needed on this side.
        return self._reading if self._reading.rss_mb is not None else None


class RemoteEngineState:
    """A reading of another engine, readable exactly where `EngineStats` is.

    Construct with `from_payload`; the seven accessors below are the ones `LiveDisplay` calls, and
    the four adapter properties are the collaborators it takes.
    """

    def __init__(self, state: DashboardState, *,
                 version: str = '',
                 snapshot_at: Optional[datetime] = None,
                 engine_started_at: Optional[datetime] = None,
                 journal_named: Optional[bool] = None) -> None:
        self._state = state
        self._version = version
        self._snapshot_at = snapshot_at
        self._engine_started_at = engine_started_at
        self._journal_named = journal_named

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> 'RemoteEngineState':
        """Rebuild from `GET /v1/dashboard/{name}`.

        The state half is walked back structurally (`from_jsonable`), so a measurement the engine
        added reaches this screen with no edit here. The header half is read by name because it IS
        the contract — those four are what a viewer cannot draw honestly without.
        """
        return cls(from_jsonable(DashboardState, payload.get('state') or {}),
                   version=payload.get('version', ''),
                   snapshot_at=_instant(payload.get('snapshot_at')),
                   engine_started_at=_instant(payload.get('engine_started_at')),
                   journal_named=payload.get('journal_named'))

    # --- the shape `LiveDisplay` reads -------------------------------------------------------

    def sources(self) -> Dict[str, Optional[SourcesSnapshot]]:
        return self._state.sources

    def ingest(self) -> Dict[str, Optional[IngestSnapshot]]:
        return self._state.ingest

    def retrieval(self) -> Dict[str, Optional[RetrievalSnapshot]]:
        return self._state.retrieval

    def llm(self) -> Dict[str, Optional[LlmSnapshot]]:
        return self._state.llm

    def breaking(self) -> BreakingSnapshot:
        return self._state.breaking

    def recent_breaking(self) -> List[BreakingRecord]:
        return self._state.recent_breaking

    def events(self) -> List[StreamEvent]:
        return self._state.events

    # --- the collaborators, or None where the engine had none --------------------------------

    def budget(self) -> Optional[_RemoteBudget]:
        return _RemoteBudget(self._state.budget) if self._state.budget is not None else None

    def watchdog(self) -> Optional[_RemoteWatchdog]:
        return _RemoteWatchdog(self._state.stalled) if self._state.stalled is not None else None

    def gauge(self) -> Optional[_RemoteGauge]:
        return _RemoteGauge(self._state.resources) if self._state.resources is not None else None

    def states_provider(self) -> Optional[Any]:
        """A callable returning `WorkerState`s, or None where the engine reported no supervisor.

        Real `WorkerState` objects rather than stand-ins, which is why the payload carries `kind` and
        `interval_seconds`: the alternative is filling them with invented values, and an invented
        number in an object is one somebody renders later.
        """
        workers = self._state.workers
        if workers is None:
            return None
        states = [WorkerState(name=worker.name, kind=worker.kind,
                              interval_seconds=worker.interval_seconds,
                              stopped_at=worker.stopped_at,
                              stopped_reason=worker.stopped_reason)
                  for worker in workers]
        return lambda: states

    # --- the header half ----------------------------------------------------------------------

    def version(self) -> str:
        return self._version

    def snapshot_at(self) -> Optional[datetime]:
        return self._snapshot_at

    def engine_started_at(self) -> Optional[datetime]:
        return self._engine_started_at

    def journal_named(self) -> Optional[bool]:
        return self._journal_named

    def worker_count(self) -> int:
        return len(self._state.workers) if self._state.workers is not None else 0


def _instant(value: Any) -> Optional[datetime]:
    """The `Z` form the engine writes, back to an aware datetime; None stays None."""
    if not isinstance(value, str):
        return value
    return datetime.fromisoformat(value.replace('Z', '+00:00'))
