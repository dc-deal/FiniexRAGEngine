"""Source-health store (ISSUE_11) — one row per feed, capturing every poll.

Follows CLAUDE.md "capture at the call, report from the store": the ingest worker records each
poll (success *and* failure) into a rolling per-source row here; the Sources report and the
weekly aggregate read it back — no log parsing. A feed that keeps failing (rate-limit, malformed
body, TLS drop) is flagged and quarantined for a cool-off window, then retried; the last few
warnings/errors are kept inline so the row is debugging-ready on its own.

Identity is the config `source_id` (joins to `articles.source_id`; one row = one poller). A
normalized `host` rides along so the report can group the same feed appearing under different
source-sets.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.config_types.app_config_types import SourceHealthConfig
from finiexragengine.types.ingest_types import HealthOutcome, SourceHealthState

logger = logging.getLogger(__name__)


class SourceHealthStore:
    """Persists per-source poll health and owns the flag/quarantine policy.

    Long-lived on the ingest worker (one instance per source-set ingestor), so the quarantine
    state is cached in memory — `should_poll` answers without a DB round-trip on the hot path;
    the cache is refreshed on every record and loaded once at construction.
    """

    def __init__(self, database_url: str, config: SourceHealthConfig,
                 table: str = 'source_health') -> None:
        self._database_url = database_url
        self._config = config
        self._TABLE = table
        # source_id -> quarantine expiry (in memory; the DB row is the source of truth).
        self._quarantined: Dict[str, datetime] = {}
        self._load_quarantines()

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the health store: {exc}') from exc

    def _load_quarantines(self) -> None:
        """Warm the in-memory quarantine cache from the DB (survives a worker restart)."""
        now = datetime.now(timezone.utc)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(f'SELECT source_id, quarantined_until FROM {self._TABLE} '
                            'WHERE quarantined_until IS NOT NULL')
                self._quarantined = {sid: until for sid, until in cur.fetchall()
                                     if until and until > now}
        except psycopg.Error as exc:
            raise VectorStoreError(f'source_health load failed: {exc}') from exc

    def should_poll(self, source_id: str) -> bool:
        """False while the source is quarantined (in-memory check — no DB hit on the hot path)."""
        until = self._quarantined.get(source_id)
        if until is None:
            return True
        if until > datetime.now(timezone.utc):
            return False
        # Cool-off elapsed — drop it and let the next poll retry (re-flags if still failing).
        self._quarantined.pop(source_id, None)
        return True

    def states_of(self, source_ids: Set[str]) -> Dict[str, SourceHealthState]:
        """The current health state of the given sources — the rows a reach decision reads.

        Reports facts, judges nothing: whether a state counts as "delivering" (and how to say so
        to a human) is `SourceReach`'s call, not the store's.

        Deliberately a live query, never the in-memory quarantine cache: the reader is usually a
        *different instance* from the writer — in worker mode the ingest worker owns acquisition
        and this store belongs to an eval runner, so a cache warmed at construction would answer
        from whenever that runner was assembled. One small SELECT per pipeline run is nothing
        against the eval cadence.

        A source with no row is simply absent from the result — it has never been polled.
        """
        if not source_ids:
            return {}
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT source_id, consecutive_failures, quarantined_until, '
                    f'last_error_type, last_status FROM {self._TABLE} WHERE source_id = ANY(%s)',
                    (list(source_ids),))
                return {row[0]: SourceHealthState(source_id=row[0], consecutive_failures=row[1],
                                                  quarantined_until=row[2], last_error_type=row[3],
                                                  last_status=row[4])
                        for row in cur.fetchall()}
        except psycopg.Error as exc:
            raise VectorStoreError(f'source health state query failed: {exc}') from exc

    def quarantined_until(self, source_id: str) -> Optional[datetime]:
        """When the source's cool-off ends, or None if it is not quarantined.

        Lets a caller that just got `should_poll() is False` say *how long* the skip lasts
        instead of only that it happened — a skip with no end date reads like a broken feed.
        """
        return self._quarantined.get(source_id)

    def record_success(self, source_id: str, host: str, source_set: str,
                       status: int = 200) -> bool:
        """Record a healthy poll. Clears any flag/quarantine (recovery). Returns True if the
        source had been flagged before — so the worker can log a one-line recovery notice."""
        now = datetime.now(timezone.utc)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(f'SELECT flagged FROM {self._TABLE} WHERE source_id = %s', (source_id,))
                row = cur.fetchone()
                was_flagged = bool(row[0]) if row else False
                cur.execute(
                    f'INSERT INTO {self._TABLE} (source_id, host, source_set, total_polls, '
                    'total_success, last_success_at, last_status, updated_at) '
                    'VALUES (%s, %s, %s, 1, 1, %s, %s, %s) '
                    'ON CONFLICT (source_id) DO UPDATE SET '
                    'host = EXCLUDED.host, source_set = EXCLUDED.source_set, '
                    f'total_polls = {self._TABLE}.total_polls + 1, '
                    f'total_success = {self._TABLE}.total_success + 1, '
                    'consecutive_failures = 0, last_success_at = EXCLUDED.last_success_at, '
                    'last_status = EXCLUDED.last_status, flagged = FALSE, flagged_at = NULL, '
                    'quarantined_until = NULL, updated_at = EXCLUDED.updated_at',
                    (source_id, host, source_set, now, status, now))
        except psycopg.Error as exc:
            raise VectorStoreError(f'source_health success record failed: {exc}') from exc
        self._quarantined.pop(source_id, None)
        return was_flagged

    def record_failure(self, source_id: str, host: str, source_set: str, *,
                       error_type: str, status: Optional[int], message: str) -> HealthOutcome:
        """Record a failed poll: bump counters, append a capped event, and flag+quarantine once
        the consecutive-failure threshold is crossed. Returns the outcome for log-level choice."""
        now = datetime.now(timezone.utc)
        event = {'ts': now.isoformat(), 'level': _level_for(error_type),
                 'type': error_type, 'status': status, 'message': message[:300]}
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT consecutive_failures, flagged, recent_events FROM {self._TABLE} '
                    'WHERE source_id = %s', (source_id,))
                row = cur.fetchone()
                consecutive = (row[0] if row else 0) + 1
                already_flagged = bool(row[1]) if row else False
                events: List[dict] = list(row[2]) if row and row[2] else []
                events.append(event)
                events = events[-self._config.recent_events_kept:]   # keep the last N (overview)

                flag = consecutive >= self._config.flag_after_consecutive_failures
                just_flagged = flag and not already_flagged
                quarantined_until = (now + timedelta(hours=self._config.quarantine_hours)
                                     if flag else None)
                flagged_at = now if just_flagged else None

                cur.execute(
                    f'INSERT INTO {self._TABLE} (source_id, host, source_set, total_polls, '
                    'total_failures, consecutive_failures, last_failure_at, last_status, '
                    'last_error_type, flagged, flagged_at, quarantined_until, recent_events, '
                    'updated_at) VALUES (%s, %s, %s, 1, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
                    'ON CONFLICT (source_id) DO UPDATE SET '
                    'host = EXCLUDED.host, source_set = EXCLUDED.source_set, '
                    f'total_polls = {self._TABLE}.total_polls + 1, '
                    f'total_failures = {self._TABLE}.total_failures + 1, '
                    'consecutive_failures = EXCLUDED.consecutive_failures, '
                    'last_failure_at = EXCLUDED.last_failure_at, last_status = EXCLUDED.last_status, '
                    'last_error_type = EXCLUDED.last_error_type, flagged = EXCLUDED.flagged, '
                    # Keep the first flag time across a quarantine streak; set it only when newly flagged.
                    f'flagged_at = COALESCE({self._TABLE}.flagged_at, EXCLUDED.flagged_at), '
                    'quarantined_until = EXCLUDED.quarantined_until, '
                    'recent_events = EXCLUDED.recent_events, updated_at = EXCLUDED.updated_at',
                    (source_id, host, source_set, consecutive, now, status, error_type,
                     flag, flagged_at, quarantined_until, json.dumps(events), now))
        except psycopg.Error as exc:
            raise VectorStoreError(f'source_health failure record failed: {exc}') from exc

        if quarantined_until is not None:
            self._quarantined[source_id] = quarantined_until
        return HealthOutcome(consecutive, just_flagged, quarantined_until)


def _level_for(error_type: str) -> str:
    """Map an error type to a warn/error level for the recent-events overview.

    Transient / external throttling is a warning (we back off and retry); a broken body or a
    hard HTTP status is an error (the feed itself is wrong)."""
    return 'warning' if error_type in ('RATE_LIMITED', 'UNREACHABLE') else 'error'
