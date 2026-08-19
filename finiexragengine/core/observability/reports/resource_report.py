"""Resource trend (ISSUE_89) — the weekly line that turns samples into a signal.

A single resource reading never meant anything: 1,191 MB on 2026-08-01 was alarming only because
there was nothing to compare it to. What answers *"is this process growing?"* is the **delta
against the previous window**, which is why this builder always reads two of them.

Kept out of `weekly_report.py` — unlike `StorageStats` (three counts in the composition itself)
this does real aggregation over two windows and is worth testing on its own. Rendered as one line;
a full table would imply a precision a 60-second sample does not have.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from finiexragengine.core.observability.resource_sample_store import ResourceSampleStore
from finiexragengine.types.resource_types import ResourceSample


@dataclass
class ResourceStats:
    """One window's resource profile, next to the same window a period earlier."""
    samples: int
    rss_mean: Optional[float] = None
    rss_min: Optional[float] = None
    rss_max: Optional[float] = None
    sockets_last: Optional[int] = None
    threads_last: Optional[int] = None
    # The previous window's mean, when there was one. None = first window; the renderer says so
    # rather than printing a delta against zero, which would read as a 400 MB jump on week one.
    rss_mean_previous: Optional[float] = None

    @property
    def rss_delta(self) -> Optional[float]:
        if self.rss_mean is None or self.rss_mean_previous is None:
            return None
        return self.rss_mean - self.rss_mean_previous

    @property
    def measured(self) -> bool:
        return self.samples > 0


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def summarise(window: Sequence[ResourceSample],
              previous: Sequence[ResourceSample]) -> ResourceStats:
    """Fold two windows of samples into the weekly shape — the DB-free core (tested).

    Sockets and threads report their **last** value rather than a mean: they are counts of live
    objects, so "24 right now" is the fact, while an average over a week that included a restart
    would describe a process that never existed.
    """
    rss = [sample.rss_mb for sample in window]
    last = window[-1] if window else None
    return ResourceStats(
        samples=len(window),
        rss_mean=_mean(rss),
        rss_min=min(rss) if rss else None,
        rss_max=max(rss) if rss else None,
        sockets_last=last.open_sockets if last else None,
        threads_last=last.threads if last else None,
        rss_mean_previous=_mean([sample.rss_mb for sample in previous]))


def build_resource_stats(store: ResourceSampleStore, since: datetime,
                         until: datetime) -> ResourceStats:
    """Read the window and the one before it, then summarise.

    The store swallows its own errors and answers `[]` for a missing table, so a weekly report on
    a database from before migration 008 renders "not sampled" instead of failing.
    """
    span = until - since
    window: List[ResourceSample] = store.window(since, until)
    previous: List[ResourceSample] = store.window(since - span, since)
    return summarise(window, previous)


def format_resource_line(stats: ResourceStats) -> str:
    """One line for the weekly report — the shared vocabulary, no table."""
    if not stats.measured:
        return ('resources — not sampled in this window (gauge disabled, psutil missing, '
                'or migration 008 not applied)')
    parts = [f'rss {stats.rss_mean:.0f} MB '
             f'(min {stats.rss_min:.0f} · max {stats.rss_max:.0f})']
    parts.append(f'sockets {stats.sockets_last}' if stats.sockets_last is not None
                 else 'sockets n/a')
    parts.append(f'threads {stats.threads_last}' if stats.threads_last is not None
                 else 'threads n/a')
    delta = stats.rss_delta
    # The delta is the point of the line; without a prior window, say so rather than imply one.
    parts.append(f'vs last week {delta:+.0f} MB rss' if delta is not None else 'first week')
    return 'resources — ' + ' · '.join(parts)
