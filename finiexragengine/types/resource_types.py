"""Process-resource domain types (ISSUE_89) — what the engine costs the machine it runs on.

The shape crosses three units: the gauge reads it, the store persists it, and the weekly report
aggregates it back. Behaviour lives in `core/observability/`; only the shape lives here.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


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
