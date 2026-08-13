"""Shared live state for the engine's terminal dashboard (ISSUE_26).

The write side of the live display: the workers push one immutable snapshot per pipeline stage
(SOURCES / INGEST / RETRIEVAL / LLM / BREAKING) plus a bounded stream of activity events; the
`LiveDisplay` render loop reads it on an interval. Two units, one per file — aggregation here,
rendering next door.

**Thread-safety, and where it comes from.** Our passes run in worker threads (`asyncio.to_thread`
in the ingest/eval workers, because feeds/OpenAI/psycopg are sync) while the render loop reads on
the event loop. Two different regimes:

- **The stage snapshots need no lock.** Each is a fully-built immutable object swapped into a
  pre-registered key in one assignment — atomic under the GIL, so a reader never sees a
  half-written stage and the render loop can iterate without one. The event stream is a
  `deque(maxlen=N)` whose `append` is thread-safe in its own right.
- **The breaking counters do**, because they are read-modify-write. They used to lean on the
  workers' shared `pass_lock` for that — which is exactly the coupling ISSUE_74 removed (that lock
  is what turned one hung feed into a nine-day outage, so it had to go). This module now owns a
  small lock of its own, held only by the three accumulating writers. That is the right home
  anyway: an invariant should be guarded where it lives, not by a lock in another domain that
  happens to serialize its callers.
"""
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple


# --- per-stage snapshots -------------------------------------------------------------------
# Built and consumed inside the display domain (the worker fills them, the renderer reads them),
# so they live with this module rather than in types/ (CLAUDE.md: a self-contained shape stays
# with its unit). Frozen = the immutability the lock-free swap relies on. Every row carries a
# `last` so the blindness test works: a dead engine shows as all ages growing together — absence
# becomes visible because it *ages*, not because a line goes missing.

@dataclass(frozen=True)
class SourcesSnapshot:
    """SOURCES row — liveness of the feed fetches (state, not history: ~56/min)."""
    last: datetime
    ok: int                                      # sources that polled ok this pass
    total: int                                   # sources the pass considered
    deviations: List[str] = field(default_factory=list)   # named problem feeds only (exception density)


@dataclass(frozen=True)
class IngestSnapshot:
    """INGEST row — what the last acquisition pass did."""
    last: datetime
    fetched: int
    new: int                                     # newly stored (genuinely new ids)
    cost_usd: float
    duration_ms: float
    suspended: bool = False                      # paid embedding suspended (provider quota, ISSUE_47)
    # Tokens actually sent to the embedder this pass (ISSUE_79). Carries the signal the money
    # cannot: embedding spend rounds to $0.000000 at display precision on every quiet pass, so
    # the cost field alone left this row saying nothing. The LLM row has shown tokens all along.
    tokens: int = 0
    truncated: int = 0                           # inputs trimmed to the model's limit


@dataclass(frozen=True)
class RetrievalSnapshot:
    """RETRIEVAL row — folded state off the eval pass (no clock of its own)."""
    last: datetime
    retrieved: int                               # articles_relevant across symbols
    symbols: int


@dataclass(frozen=True)
class LlmSnapshot:
    """LLM row — the last eval pass's spend, per-symbol signals, and the analysis-unit count."""
    last: datetime
    tokens: int
    cost_usd: float
    duration_ms: float
    # (symbol, signal, base_currency, group) — `group` is the retrieval query, the analysis-unit key
    # the display merges fanned same-query symbols by (ISSUE_70); base is for the merged chip label.
    signals: List[Tuple[str, str, str, str]] = field(default_factory=list)
    calls: int = 0                          # LLM analysis units (unique queries); < len(signals) if grouped


@dataclass(frozen=True)
class BreakingSnapshot:
    """BREAKING row — cumulative over the session (detected by ingest, confirmed by eval)."""
    last: Optional[datetime]
    detected: int                                # candidates flagged (ISSUE_11 ingest side)
    confirmed: int                               # confirmed breaking EPISODES (eval side, edge-triggered)
    detail: str = ''                             # last reaction time, e.g. 'engine 42s / e2e 3.1m'


@dataclass
class BreakingRecord:
    """One recent confirmed episode — for the BREAKING section (newest kept, oldest drops).

    `last_seen` advances every pass the symbol re-breaks (`touch_breaking_episode`), so the renderer
    tells a live episode (`now − last_seen ≤ EPISODE_GAP`) from an ended one and shows a duration.
    Deliberately **not frozen** (unlike the stage snapshots): `last_seen` is updated in place — a
    single atomic reference assignment under the GIL, and every writer runs under this module's
    own counter lock (ISSUE_74), so a reader never sees a torn value.
    """
    started: datetime
    last_seen: datetime
    symbol: str
    signal: str
    reason: str = ''                             # why it broke (the LLM's reasoning; ISSUE_64)


@dataclass(frozen=True)
class StreamEvent:
    """One line in the activity stream — history, the only region that grows."""
    ts: datetime
    stage: str                                   # 'INGEST' | 'LLM' | 'SOURCE' | 'BUDGET' | 'BREAKING'
    message: str


class EngineStats:
    """The shared live state — written by the workers, read by `LiveDisplay` (ISSUE_26).

    One snapshot **per worker** and stage: sources/ingest keyed by source-set id, retrieval/llm
    keyed by pipeline id — because N ingest + M eval workers run concurrently and a single slot
    per stage would let them clobber each other (last-writer-wins). Plus a bounded activity deque.
    The BUDGET row is not held here: it is read live from the `BudgetGuard` at render time, which
    is already a queryable source (CLAUDE.md — report from the live source, do not duplicate).

    Keys are **pre-registered** from the known worker ids, so the dicts never resize after
    construction — a worker only ever reassigns its own key (atomic under the GIL). That keeps the
    render loop's iteration lock-free (no 'dict changed size during iteration' race), which is the
    whole point of the design.
    """

    def __init__(self, *, source_set_ids: Optional[List[str]] = None,
                 pipeline_ids: Optional[List[str]] = None,
                 max_events: int = 200) -> None:
        # Pre-register every worker's key with a None snapshot — the renderer shows a dim idle row
        # until that worker's first pass fills it. Insertion order = display order.
        self._sources: Dict[str, Optional[SourcesSnapshot]] = {
            source_set_id: None for source_set_id in (source_set_ids or [])}
        self._ingest: Dict[str, Optional[IngestSnapshot]] = {
            source_set_id: None for source_set_id in (source_set_ids or [])}
        self._retrieval: Dict[str, Optional[RetrievalSnapshot]] = {
            pipeline_id: None for pipeline_id in (pipeline_ids or [])}
        self._llm: Dict[str, Optional[LlmSnapshot]] = {
            pipeline_id: None for pipeline_id in (pipeline_ids or [])}
        # Breaking is session-cumulative and engine-wide (detected by any ingest, confirmed by any
        # eval) — one global row, not per worker. Starts at zero, never None.
        self._breaking: BreakingSnapshot = BreakingSnapshot(last=None, detected=0, confirmed=0)
        # The last few confirmed episodes (edge-triggered, ISSUE_11) for the RECENT summary line.
        self._recent_breaking: Deque[BreakingRecord] = deque(maxlen=6)
        # Bounded history: O(1) memory regardless of uptime; oldest events fall off the back.
        self._events: Deque[StreamEvent] = deque(maxlen=max_events)
        # Guards ONLY the read-modify-write counters below (ISSUE_74) — the snapshot setters stay
        # lock-free on purpose, that is the design the render loop depends on. Contention is nil:
        # microseconds of work, a handful of writers on minute cadences.
        self._counter_lock = threading.Lock()

    # --- writers (called from worker threads) ------------------------------------------------

    def set_sources(self, source_set_id: str, snapshot: SourcesSnapshot) -> None:
        self._sources[source_set_id] = snapshot  # reassign a pre-registered key = no resize

    def set_ingest(self, source_set_id: str, snapshot: IngestSnapshot) -> None:
        self._ingest[source_set_id] = snapshot

    def set_retrieval(self, pipeline_id: str, snapshot: RetrievalSnapshot) -> None:
        self._retrieval[pipeline_id] = snapshot

    def set_llm(self, pipeline_id: str, snapshot: LlmSnapshot) -> None:
        self._llm[pipeline_id] = snapshot

    def add_breaking_detected(self, count: int, *, at: datetime) -> None:
        """Ingest flagged `count` candidates — bump the cumulative detected total."""
        with self._counter_lock:                          # read-modify-write (ISSUE_74)
            current = self._breaking
            self._breaking = BreakingSnapshot(last=at, detected=current.detected + count,
                                              confirmed=current.confirmed, detail=current.detail)

    def add_breaking_episode(self, symbol: str, signal: str, reason: str, detail: str, *,
                             at: datetime) -> None:
        """One confirmed breaking episode (edge-triggered, ISSUE_11): bump the episode count, set
        the reaction detail, and record it (with its reason) for the BREAKING section (ISSUE_64)."""
        with self._counter_lock:                          # read-modify-write (ISSUE_74)
            current = self._breaking
            self._breaking = BreakingSnapshot(last=at, detected=current.detected,
                                              confirmed=current.confirmed + 1, detail=detail)
            self._recent_breaking.append(BreakingRecord(started=at, last_seen=at, symbol=symbol,
                                                        signal=signal, reason=reason))

    def touch_breaking_episode(self, symbol: str, *, at: datetime) -> None:
        """A symbol still breaking this pass (same ongoing episode, ISSUE_64): advance its record's
        `last_seen` so the renderer keeps it 'live' and grows its duration. A no-op if the episode's
        start already dropped off the bounded deque — the count already carries it."""
        # Under the counter lock too: it walks the same deque `add_breaking_episode` appends to.
        with self._counter_lock:
            for record in reversed(self._recent_breaking):  # newest match = the open episode
                if record.symbol == symbol:
                    record.last_seen = at
                    return

    def push_event(self, stage: str, message: str) -> None:
        """Append one activity line (thread-safe deque.append); oldest falls off at maxlen."""
        self._events.append(StreamEvent(datetime.now(timezone.utc), stage, message))

    # --- readers (render loop) -------------------------------------------------------------

    def sources(self) -> Dict[str, Optional[SourcesSnapshot]]:
        return self._sources

    def ingest(self) -> Dict[str, Optional[IngestSnapshot]]:
        return self._ingest

    def retrieval(self) -> Dict[str, Optional[RetrievalSnapshot]]:
        return self._retrieval

    def llm(self) -> Dict[str, Optional[LlmSnapshot]]:
        return self._llm

    def breaking(self) -> BreakingSnapshot:
        return self._breaking

    def recent_breaking(self) -> List[BreakingRecord]:
        """The last few confirmed episodes (oldest→newest) for the RECENT summary line."""
        return list(self._recent_breaking)

    def events(self) -> List[StreamEvent]:
        """A stable copy for the renderer — iterating the live deque under append is avoided."""
        return list(self._events)
