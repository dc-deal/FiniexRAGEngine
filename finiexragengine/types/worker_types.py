"""Runtime state of a background worker (ISSUE_10) — surfaced via /health."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class WorkerState:
    """One worker's live status — what /health (and later the live display, #26) shows."""
    name: str                    # 'ingest:crypto_news' | 'eval:crypto_sentiment'
    kind: str                    # 'ingest' | 'eval'
    interval_seconds: int
    runs: int = 0
    last_status: str = 'pending'          # pending | ok | error
    last_run_at: Optional[datetime] = None
    last_duration_ms: float = 0.0
    last_detail: str = ''                 # compact pass summary or error message
