"""Runtime state of a background worker (ISSUE_10) — surfaced via /health."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class WorkerState:
    """One worker's live status — what /health (and later the live display, #26) shows."""
    name: str                    # 'ingest:crypto_news' | 'eval:crypto_sentiment'
    kind: str                    # 'ingest' | 'eval'
    interval_seconds: int        # cadence in seconds (for eval, derived from `timeframe`)
    timeframe: Optional[str] = None       # eval bar-close frame ('M10'); None for ingest
    runs: int = 0
    last_status: str = 'pending'          # pending | ok | error
    last_run_at: Optional[datetime] = None
    last_duration_ms: float = 0.0
    last_detail: str = ''                 # compact pass summary or error message
    # Set when the worker's task ended while the supervisor was still running (ISSUE_82 follow-up).
    # A healthy worker loops until `stop_all()`, so a task that finishes on its own is always a
    # defect — and used to be an invisible one: the exception sat unretrieved in a Task nobody
    # looked at. These two make it a fact the API and the dashboard can both read.
    stopped_at: Optional[datetime] = None
    stopped_reason: str = ''              # exception repr, or why it returned
