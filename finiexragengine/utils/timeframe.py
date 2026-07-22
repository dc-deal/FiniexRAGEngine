"""Trading-style timeframes for bar-close-aligned eval cadence (ISSUE_timeframe).

Pure and dependency-free (mirrors `archive_layout.py`): the eval trigger waits until the
next wall-clock bar close instead of a relative interval, so signal snapshots land on a
fixed, restart-independent grid (`:00/:10` for M10, midnight UTC for D1) that pairs 1:1 with
the timeframe bar the consumer reasons in. UTC-only — a naive datetime is a bug, not a guess.

This governs *when* an eval fires (the cadence), never how far the retrieval looks back
(`recency_window_minutes`, a separate knob): news is sparse, so lookback is decoupled from
cadence on purpose.
"""
import math
from datetime import datetime, timezone
from typing import Dict, List

# Name -> length in minutes. The established MetaTrader frames plus M10 (the 600s cadence the
# engine shipped with). Each divides evenly into a 1440-minute day, so every boundary lands on
# the Unix-epoch/day grid (epoch 0 is midnight UTC) — no per-frame offset table needed.
TIMEFRAMES: Dict[str, int] = {
    'M1': 1, 'M5': 5, 'M10': 10, 'M15': 15, 'M30': 30,
    'H1': 60, 'H4': 240, 'D1': 1440,
}


def timeframe_minutes(timeframe: str) -> int:
    """Length of `timeframe` in minutes; raises on an unknown name."""
    try:
        return TIMEFRAMES[timeframe]
    except KeyError:
        raise ValueError(
            f'unknown timeframe {timeframe!r} — supported: {", ".join(TIMEFRAMES)}') from None


def supported_timeframes() -> List[str]:
    """The supported names, shortest first — for docs / CLI listing / validation."""
    return list(TIMEFRAMES)


def next_boundary(now: datetime, timeframe: str) -> datetime:
    """The next bar close strictly after `now`, aligned to the epoch/day grid (UTC).

    Epoch 0 is midnight UTC, so epoch multiples of the bar length are exactly the bar closes
    (`:00/:10/...` for M10, `00/04/.../20:00` for H4, `00:00` for D1). "Strictly after" means a
    `now` sitting exactly on a boundary advances to the following one — that bar just ran.
    """
    now = _require_utc(now)
    step = timeframe_minutes(timeframe) * 60          # bar length in seconds
    next_epoch = (math.floor(now.timestamp() / step) + 1) * step
    return datetime.fromtimestamp(next_epoch, tz=timezone.utc)


def seconds_until_next_boundary(now: datetime, timeframe: str) -> float:
    """Seconds from `now` to the next bar close — what the aligned eval trigger waits."""
    return (next_boundary(now, timeframe) - _require_utc(now)).total_seconds()


def _require_utc(moment: datetime) -> datetime:
    """Reject naive datetimes, normalise aware ones to UTC (mirrors archive_layout)."""
    if moment.tzinfo is None:
        raise ValueError('timeframe math needs a timezone-aware UTC datetime, got naive')
    return moment.astimezone(timezone.utc)
