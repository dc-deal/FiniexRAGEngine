"""Resource sample store (ISSUE_89) — the durable half of the process gauge.

`source_poll_log`'s shape applied to the process instead of the feeds, and it inherits that unit's
two rules verbatim:

- **A write never fails the caller.** Every DB error is logged and swallowed. The gauge runs on the
  stall watchdog's tick, and a diagnostic that can take the watchdog down would be its own irony —
  the same reasoning that made the poll journal swallow while `source_health` raises. Nothing in
  the engine's behaviour reads this table.
- **Pruned once per UTC day by the writer**, retention `diagnostics.resource_retention_days` (14),
  matching the poll journal and the rotating file log so an incident and its resource history age
  out together.

Volume is negligible: one row per 60s tick is ~1.4k/day against the poll journal's ~51k.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import psycopg

from finiexragengine.types.resource_types import ResourceSample

logger = logging.getLogger(__name__)


class ResourceSampleStore:
    """Appends process samples to `resource_samples` and prunes the series once per UTC day."""

    def __init__(self, database_url: str, *, retention_days: int = 14,
                 table: str = 'resource_samples') -> None:
        self._database_url = database_url
        self._retention_days = retention_days
        self._TABLE = table
        # The UTC day the last prune ran for. None = not pruned in this process yet, so the first
        # sample of the process also prunes — a server that restarts daily still keeps the table
        # bounded, and one that runs for months prunes exactly once a day.
        self._pruned_on: Optional[date] = None

    def record(self, sample: ResourceSample) -> None:
        """Append one sample. Prunes first when the UTC day has turned."""
        # Keyed on the wall clock, not on `sample.ts`: the two are the same in production, but a
        # backdated sample must not move the day bookkeeping and trigger an extra prune. Same
        # anchor `source_poll_log` uses. Pruning before the insert (not after) keeps the
        # day-boundary logic to a single branch.
        today = datetime.now(timezone.utc).date()
        if self._pruned_on != today:
            self._pruned_on = today
            self.prune()
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cur.execute(
                    f'INSERT INTO {self._TABLE} (ts, rss_mb, open_sockets, threads) '
                    'VALUES (%s, %s, %s, %s)',
                    (sample.ts, sample.rss_mb, sample.open_sockets, sample.threads))
        except psycopg.Error as exc:
            logger.warning('resource sample write failed (diagnostics only, tick continues): %s',
                           exc)

    def prune(self) -> int:
        """Delete samples older than the retention window; returns how many rows went."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cur.execute(f'DELETE FROM {self._TABLE} WHERE ts < %s', (cutoff,))
                deleted = cur.rowcount
        except psycopg.Error as exc:
            logger.warning('resource sample prune failed (diagnostics only): %s', exc)
            return 0
        if deleted:
            logger.info('resource samples pruned %d row(s) older than %d days',
                        deleted, self._retention_days)
        return deleted

    def window(self, since: datetime, until: Optional[datetime] = None) -> List[ResourceSample]:
        """Every sample in the window, oldest first — what the weekly aggregate reads back.

        Returns an empty list rather than raising when the table is missing or unreachable: this
        is a report input, and a weekly report that dies because a diagnostic table is absent is
        the same failure the swallow rule above prevents on the write side.
        """
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                            (self._TABLE,))
                if cur.fetchone()[0] == 0:
                    return []
                if until is None:
                    cur.execute(f'SELECT ts, rss_mb, open_sockets, threads FROM {self._TABLE} '
                                'WHERE ts >= %s ORDER BY ts', (since,))
                else:
                    cur.execute(f'SELECT ts, rss_mb, open_sockets, threads FROM {self._TABLE} '
                                'WHERE ts >= %s AND ts < %s ORDER BY ts', (since, until))
                return [ResourceSample(ts=row[0], rss_mb=float(row[1]),
                                       open_sockets=row[2], threads=row[3])
                        for row in cur.fetchall()]
        except psycopg.Error as exc:
            logger.warning('resource sample read failed (diagnostics only): %s', exc)
            return []
