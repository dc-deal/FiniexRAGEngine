"""Process-resource domain types (ISSUE_89) — what the engine costs the machine it runs on.

The shape crosses three units: the gauge reads it, the store persists it, and the weekly report
aggregates it back. Behaviour lives in `core/observability/`; only the shape lives here.

The memory census shapes (2026-09-30) cross the same way: the census builds them, the gauge logs
the light reading on its tick, and the diagnose route serves the full diagnosis.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class ResourceSample:
    """One reading of the running process, taken on the stall-watchdog tick.

    `open_sockets` and `threads` are optional for the same reason and not the same one:

    - **sockets** can be *refused*. `psutil.Process().net_connections()` needs privileges some
      platforms do not grant (Windows, containers with a restricted profile), and the live host is
      Windows. A refusal degrades that one field to None rather than losing the whole sample —
      resident memory is the number the 2026-08-01 incident was actually about.
    - **threads** is cheap and unprivileged everywhere, so None here means the platform surprised
      us and the sample says so instead of reporting a plausible zero.
    """
    ts: datetime
    rss_mb: float                          # resident set size — process memory, not the database
    open_sockets: Optional[int] = None
    threads: Optional[int] = None
    # Private bytes — memory the process has committed, resident or paged out. On Windows `rss` is
    # the WORKING SET, which the OS trims under pressure: on 2026-09-30 the gauge read 5.2 GB while
    # the process held 7.2 GB, i.e. `rss` under-reports exactly the failure it exists to catch.
    # None where the platform does not expose it (psutil reports it on Windows only).
    private_mb: Optional[float] = None


@dataclass
class GcGeneration:
    """One garbage-collector generation: how often it ran, and how long its runs froze the process.

    A collection holds the GIL for its whole duration, so every thread stops. On 2026-09-27..30
    full collections over a paged-out heap froze the engine for 10–18 min every ~6 h, and the log
    showed only silence. `last_pause_ms` / `max_pause_ms` are measured by a `gc.callbacks` hook and
    are None until this generation has run once since the hook was installed.
    """
    generation: int
    collections: int                       # since process start (gc.get_stats)
    collected: int
    uncollectable: int
    last_pause_ms: Optional[float] = None
    max_pause_ms: Optional[float] = None


@dataclass
class GcPause:
    """The slowest collection since the previous reading — what the tick warns about."""
    generation: int
    pause_ms: float
    collected: int


@dataclass
class MemoryReading:
    """The cheap half of the census: O(1) numbers, taken on every gauge tick.

    Nothing here walks the heap, so it can run on the event loop every minute. Growth in
    `allocated_blocks` with flat `gc` counts means live Python objects accumulate; growth in private
    bytes with flat `allocated_blocks` means native memory does (what the SSL contexts were).
    """
    ts: datetime
    allocated_blocks: int                  # sys.getallocatedblocks(): live pymalloc blocks
    gc_pending: List[int]                  # gc.get_count(): allocations toward each threshold
    generations: List[GcGeneration]
    threads: int
    # Dumper classes psycopg holds forever (`AdaptersMap._optimised`). Grew by 8 per connection
    # while pgvector was registered per connection; must stay flat now. None if psycopg moves it.
    pg_adapter_classes: Optional[int] = None
    slowest_pause: Optional[GcPause] = None


@dataclass
class TypeCount:
    """How many GC-tracked objects of one type are alive — a line of the census."""
    type_name: str
    count: int


@dataclass
class TracedLine:
    """One source line's share of traced memory (tracemalloc), and its growth since last asked."""
    location: str                          # 'path/to/file.py:123'
    size_kb: float
    count: int
    size_diff_kb: Optional[float] = None
    count_diff: Optional[int] = None


@dataclass
class MemoryDiagnosis:
    """The expensive half: walks every GC-tracked object once. On request only, never on a tick.

    `census_ms` states what the walk itself cost — it holds the GIL, so the number is how long the
    engine stood still to answer. `live` counts the objects this engine has already leaked once
    (SSL contexts, urllib openers) or whose count should stay flat (connections, threads).
    """
    reading: MemoryReading
    rss_mb: Optional[float]
    private_mb: Optional[float]
    gc_tracked_objects: int
    top_types: List[TypeCount]
    live: Dict[str, int]
    census_ms: float
    tracemalloc_enabled: bool = False
    traced_mb: Optional[float] = None
    traced_top: List[TracedLine] = field(default_factory=list)
    # Growth against the previous diagnosis — the question a leak hunt actually asks. Empty on the
    # first call after boot; `traced_since` names the snapshot the diff is measured against.
    traced_growth: List[TracedLine] = field(default_factory=list)
    traced_since: Optional[datetime] = None
