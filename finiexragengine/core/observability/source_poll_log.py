"""Source poll journal (ISSUE_76) — one row per feed poll attempt, so feed questions stop being
guesses.

`cost_log`'s shape applied to the unpaid calls: capture at the call, report from the store. Every
attempted poll lands here with the duration it took — **including the ones that failed**, which is
the whole point. `StageTimer` records nothing for a stage that raises, so before this the polls most
worth studying (the timeouts) produced no data at all: when ecb_press timed out on 2026-08-15 the
engine could not say whether the feed had been slow or dead.

Deliberately a raw journal rather than pre-aggregated buckets. At ~26k rows/day it costs little, and
it answers questions nobody has asked yet — the reason the 2026-08 investigation needed five ad-hoc
queries. `reports/source_latency_report.py` reads it back with native `percentile_cont`.

Skips are **not** journaled: a floor-skip or a quarantine skip never reached the feed and has
nothing to time. Their signal lives in the *gaps* between rows, which also catches worker death and
config changes — things a quarantine-only row would miss.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import psycopg

from finiexragengine.types.ingest_types import PollSample

logger = logging.getLogger(__name__)


class SourcePollLog:
    """Appends poll samples to `source_poll_log` and prunes the journal once per UTC day.

    Long-lived on the ingest worker (one per ingestor, beside `SourceHealthStore`), so the
    prune bookkeeping is a single in-memory date.

    **A journal write never fails a pass.** Every DB error here is logged and swallowed: this unit
    exists to explain outages, and turning it into a new cause of one would be its own irony. That
    is the opposite of `SourceHealthStore`, which raises — health drives the *reach decision*, so
    losing it silently would corrupt the engine's behaviour; losing a diagnostic row only costs a
    sample.
    """

    def __init__(self, database_url: str, *, retention_days: int = 14,
                 table: str = 'source_poll_log') -> None:
        self._database_url = database_url
        self._retention_days = retention_days
        self._TABLE = table
        # The UTC day the last prune ran for. None = not pruned in this process yet, so the first
        # record of the process also prunes — a worker that restarts daily still keeps the journal
        # bounded, and one that runs for months prunes exactly once a day.
        self._pruned_on: Optional[date] = None

    def record(self, sample: PollSample) -> None:
        """Append one attempted poll. Prunes first when the UTC day has turned."""
        now = datetime.now(timezone.utc)
        # One comparison per poll on the hot path; one DELETE per worker per day. Pruning before
        # the insert (not after) keeps the day-boundary logic to a single branch.
        if self._pruned_on != now.date():
            self._pruned_on = now.date()
            self.prune()
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'INSERT INTO {self._TABLE} (ts, source_id, source_set, outcome, '
                    'duration_ms, error_type, status, articles) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                    (now, sample.source_id, sample.source_set, sample.outcome,
                     sample.duration_ms, sample.error_type, sample.status, sample.articles))
        except psycopg.Error as exc:
            logger.warning('poll journal write failed for %s (diagnostics only, pass continues): %s',
                           sample.source_id, exc)

    def prune(self) -> int:
        """Delete samples older than the retention window; returns how many rows went."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(f'DELETE FROM {self._TABLE} WHERE ts < %s', (cutoff,))
                deleted = cur.rowcount
        except psycopg.Error as exc:
            logger.warning('poll journal prune failed (diagnostics only): %s', exc)
            return 0
        if deleted:
            logger.info('poll journal pruned %d sample(s) older than %d days',
                        deleted, self._retention_days)
        return deleted

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._database_url)
