"""The engine's live state, as one transportable object (ISSUE_126 Phase 2).

`EngineStats` is the write side of the dashboard and `LiveDisplay` the read side, and until the
engine became a service they lived in one process. They no longer do: the engine runs headless and
the console runs somewhere else, so the state has to cross a wire.

**What travels is a verdict, not the material to compute one.** `over_ceiling` is decided against
`diagnostics.resource_rss_warn_mb` and `stalled` against each worker's own cadence — both engine-local
configuration. A viewer re-deriving either from its own config is the defect the FiniexDataCollector
paid for: their renderer read a *local* file-size limit and drew a remote file of 12,737 ticks as
1274 % of a boundary that instance does not have. So the engine decides and the snapshot carries the
answer.

**An unknown is a third state, never the healthy one.** In one process the renderer's optional
collaborators default to benign — no budget guard prints `$0.000 today`, no watchdog means
`stalled_workers()` returns an empty set and every row renders healthy on the exact colour channel
the display calls *"the whole signal here"*. In one process that is correct: the collaborator is
absent because this deployment genuinely has none. Across a wire the same absence means *"the viewer
could not find out"*, and rendering that as all-clear is how a screen lies. Hence `Optional` on every
collaborator group below, with the distinction spelled out per field: `None` is "not established",
`[]` is "established, and empty".

**Structural, not a hand-listed mirror.** The snapshot is dataclasses all the way down and travels
through `utils.dataclass_json.to_jsonable`, which walks fields *and* public properties and normalises
every datetime to UTC with a `Z`. A measurement added to a stage snapshot therefore reaches a remote
screen with no converter edit — the only version of this that survives six months.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.observability.resource_gauge import ResourceGauge
from finiexragengine.core.observability.stall_watchdog import StallWatchdog
from finiexragengine.core.ui.engine_stats import (
    BreakingRecord,
    BreakingSnapshot,
    EngineStats,
    IngestSnapshot,
    LlmSnapshot,
    RetrievalSnapshot,
    SourcesSnapshot,
    StreamEvent,
)
from finiexragengine.types.worker_types import WorkerState

# The closed vocabulary of views this surface serves. One today, and the route's `{name}` segment is
# what keeps the grant model applying to it — a collection route with no identity segment is gated
# only by the surface floor and is invisible to the scope walk in tests/api/test_report_scopes.py.
DASHBOARD_VIEWS: tuple = ('engine',)


@dataclass(frozen=True)
class ResourceReading:
    """The gauge's verdict plus the numbers behind it — the verdict is the load-bearing half.

    `rss_mb` is `None` until the gauge has taken its first sample, which is a different state from
    "no gauge at all" (that one is `DashboardState.resources is None`).
    """
    over_ceiling: bool
    ceiling_mb: float
    rss_mb: Optional[float] = None
    # A real datetime, not the string `ResourceGauge.status()` hands out: that one is already
    # `.isoformat()`d, so the serializer passes it through and it arrives in `+00:00` form while
    # every other instant in this payload ends in `Z`. One field in a second datetime format is
    # exactly the kind of thing a viewer discovers at parse time.
    sampled_at: Optional[datetime] = None


@dataclass(frozen=True)
class WorkerLiveness:
    """The two fields the header's `WORKER DEAD` segment is built from, and nothing else.

    `WorkerState` carries more (runs, last_status, durations), but the renderer reads exactly these
    — so this is what the snapshot promises, rather than a whole internal shape whose other fields
    would become a contract by accident.
    """
    name: str
    stopped_at: Optional[datetime] = None
    stopped_reason: str = ''


@dataclass(frozen=True)
class DashboardState:
    """Everything the panel draws. Free to grow: the converter enumerates no fields."""
    breaking: BreakingSnapshot
    sources: Dict[str, Optional[SourcesSnapshot]] = field(default_factory=dict)
    ingest: Dict[str, Optional[IngestSnapshot]] = field(default_factory=dict)
    retrieval: Dict[str, Optional[RetrievalSnapshot]] = field(default_factory=dict)
    llm: Dict[str, Optional[LlmSnapshot]] = field(default_factory=dict)
    recent_breaking: List[BreakingRecord] = field(default_factory=list)
    events: List[StreamEvent] = field(default_factory=list)
    # Each of the four below: None = the engine has no such collaborator, so the viewer knows it
    # was not told rather than being told everything is fine.
    workers: Optional[List[WorkerLiveness]] = None
    budget: Optional[Dict[str, Any]] = None      # BudgetGuard.status(), a dynamic dict by design
    stalled: Optional[List[str]] = None          # [] means the watchdog ran and found none
    resources: Optional[ResourceReading] = None


@dataclass(frozen=True)
class DashboardSnapshot:
    """One reading of the engine, stamped by the engine's own clock.

    `snapshot_at` is the field the viewer's whole honesty rests on: without it a frozen fetch looks
    exactly like a live one, and every age on the screen would be an engine timestamp subtracted
    from the viewer's clock — two clocks, one number, no way to see the difference.
    """
    view: str
    snapshot_at: datetime
    version: str
    state: DashboardState
    # The ENGINE's start, not the renderer's. In-process the header measured the display object's
    # construction, which in a viewer would measure the viewer. None when no build info was sampled.
    engine_started_at: Optional[datetime] = None
    # Tri-state on purpose: True named, False unnamed (the warning belongs on screen), None not
    # established — the journal identity could not be resolved at all, which is a third thing.
    journal_named: Optional[bool] = None


def sample_dashboard(stats: EngineStats,
                     *,
                     view: str = 'engine',
                     version: str = '',
                     engine_started_at: Optional[datetime] = None,
                     journal_named: Optional[bool] = None,
                     budget_guard: Optional[BudgetGuard] = None,
                     stall_watchdog: Optional[StallWatchdog] = None,
                     resource_gauge: Optional[ResourceGauge] = None,
                     states_provider: Optional[Callable[[], List[WorkerState]]] = None,
                     ) -> DashboardSnapshot:
    """Read the live state once, with every verdict already taken on this side of the wire.

    Cheap by construction: the stage snapshots are immutable objects swapped into pre-registered
    keys, so reading them needs no lock and cannot see a half-written stage (`engine_stats`'s own
    thread-safety note). The deques are copied by their accessors.
    """
    resources: Optional[ResourceReading] = None
    if resource_gauge is not None:
        # `status()` rather than the individual accessors: it is what /v1/health already reports, so
        # the two surfaces cannot drift, and the ceiling lives behind it (the gauge exposes no public
        # attribute for it, and reaching for the private one is not how this codebase reads state).
        gauge = resource_gauge.status()
        sample = resource_gauge.latest()
        resources = ResourceReading(over_ceiling=bool(gauge['over_ceiling']),
                                    ceiling_mb=float(gauge['ceiling_mb']),
                                    rss_mb=gauge['rss_mb'],
                                    sampled_at=sample.ts if sample is not None else None)

    workers: Optional[List[WorkerLiveness]] = None
    if states_provider is not None:
        workers = [WorkerLiveness(name=state.name,
                                  stopped_at=state.stopped_at,
                                  stopped_reason=state.stopped_reason)
                   for state in states_provider()]

    state = DashboardState(
        breaking=stats.breaking(),
        sources=stats.sources(),
        ingest=stats.ingest(),
        retrieval=stats.retrieval(),
        llm=stats.llm(),
        recent_breaking=stats.recent_breaking(),
        events=stats.events(),
        workers=workers,
        budget=budget_guard.status() if budget_guard is not None else None,
        # sorted() so two identical readings serialize identically — an unordered set would turn a
        # diff between two fetches into noise.
        stalled=(sorted(stall_watchdog.stalled_workers())
                 if stall_watchdog is not None else None),
        resources=resources,
    )
    return DashboardSnapshot(view=view,
                             snapshot_at=datetime.now(timezone.utc),
                             version=version,
                             state=state,
                             engine_started_at=engine_started_at,
                             journal_named=journal_named)
