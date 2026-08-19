"""Source-health store (ISSUE_11, ISSUE_84) — one row per feed, capturing every poll.

Follows CLAUDE.md "capture at the call, report from the store": the ingest worker records each
poll (success *and* failure) into a rolling per-source row here; the Sources report and the
weekly aggregate read it back — no log parsing. A feed that keeps failing (rate-limit, malformed
body, TLS drop) is flagged and quarantined for a cool-off window, then probed once; the last few
warnings/errors are kept inline so the row is debugging-ready on its own.

Identity is the config `source_id` (joins to `articles.source_id`; one row = one poller). A
normalized `host` rides along so the report can group the same feed appearing under different
source-sets.

**The quarantine policy (ISSUE_84)** is the circuit-breaker shape `BudgetGuard` already owns for
paid calls, applied to the other resource the engine guards:

- a **graduated ladder** (`quarantine_hours: [1, 6, 24]`) instead of a flat 24h, because a feed at
  99.97 % availability and one that has never answered deserve different answers;
- the rung comes from the failure's **type and its measured duration** — `UNREACHABLE` is an
  overloaded bucket, and only the duration separates "went quiet" (burns the deadline, transient)
  from "refused" (returns in milliseconds, durable);
- an explicit **half-open probe** at cool-off expiry: exactly one poll decides recovery or
  escalation;
- a **correlated-failure guard**: when (nearly) every pollable source of a pass fails at once, the
  common cause is local, so nobody is quarantined and no rung advances. On 2026-07-29 the absence
  of this turned a ~5h host outage into a ~25h blackout.

The decision is deferred to the end of the pass (`pass_scope`) because the correlation ratio is
not knowable while the loop is still running. Only the *decision* is deferred — counters, streak
and the event ring are still written per failure, so a pass that dies mid-way loses nothing.
"""
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional, Set, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.config_types.app_config_types import SourceHealthConfig
from finiexragengine.types.ingest_types import HealthOutcome, HostEvent, SourceHealthState

logger = logging.getLogger(__name__)

# The two kinds of row in the episode history. A quarantine belongs to one feed; a correlated
# event belongs to a whole set and explains why its feeds were NOT quarantined.
_QUARANTINE = 'quarantine'
_CORRELATED = 'correlated'


@dataclass
class _PendingFlag:
    """A crossed failure threshold, held until the pass can say whether it means anything.

    Deliberately file-private and not in `types/`: it never crosses a seam — it exists between
    `record_failure` and the end of the enclosing `pass_scope`, both inside this store.
    """
    source_id: str
    host: str
    source_set: str
    error_type: str
    status: Optional[int]
    duration_ms: Optional[float]
    deadline_ms: Optional[float]
    streak: int
    probe: bool
    events: List[dict]          # the event ring as it stood — frozen into the episode
    # The very object `record_failure` already handed back. Completed in place when the pass
    # resolves, so the caller that stored it (the Ingestor's `health_notes`) sees the real
    # verdict without a second lookup — and cannot accidentally log the provisional one.
    outcome: HealthOutcome


@dataclass
class _PassState:
    """One ingest pass as the policy sees it: who was attempted, and who failed.

    The denominator is not handed in, it accumulates: a source that was quarantined, inside its
    poll floor or held by the set back-off records nothing at all, and every source that *was*
    attempted records exactly one outcome. So `failed + succeeded` is precisely "the sources this
    pass could poll" — with no second code path that could disagree with the loop, and without
    calling `should_poll` twice per source (which would hand out two probes).
    """
    source_set: str
    failed: Set[str] = field(default_factory=set)
    succeeded: Set[str] = field(default_factory=set)
    pending: List[_PendingFlag] = field(default_factory=list)

    @property
    def pollable(self) -> int:
        return len(self.failed) + len(self.succeeded)


class SourceHealthStore:
    """Persists per-source poll health and owns the flag/quarantine policy.

    Long-lived on the ingest worker (one instance per source-set ingestor), so the quarantine
    state is cached in memory — `should_poll` answers without a DB round-trip on the hot path;
    the cache is refreshed on every record and loaded once at construction.
    """

    def __init__(self, database_url: str, config: SourceHealthConfig,
                 table: str = 'source_health',
                 episode_table: str = 'source_quarantine_log') -> None:
        self._database_url = database_url
        self._config = config
        self._TABLE = table
        self._EPISODES = episode_table
        # The escalation ladder, never empty: a config that somehow produced [] would silently
        # mean "no quarantine at all", which is a policy change nobody asked for.
        self._ladder: List[int] = list(config.quarantine_hours) or [24]
        # source_id -> quarantine expiry (in memory; the DB row is the source of truth).
        self._quarantined: Dict[str, datetime] = {}
        # ...and the ladder position that expiry came from, cached beside it. Both are written
        # in the same moment (`_apply_flag`) and loaded in the same query at construction, so
        # asking the DB for the rung on every skipped poll bought nothing — and `should_poll`
        # promises no round-trip on the hot path. At a 15s cadence that query would have run
        # ~5,760 times a day per quarantined feed.
        self._rungs: Dict[str, Tuple[int, int]] = {}
        # Sources whose cool-off has elapsed and that are owed exactly one probe poll (ISSUE_84).
        self._probing: Set[str] = set()
        # The enclosing pass, while one is open. Safe as instance state: every Ingestor gets its
        # own store, and a trigger awaits its pass before computing the next wait — so a pass
        # cannot overlap itself (the same reasoning that let ISSUE_74 drop the shared lock).
        self._pass: Optional[_PassState] = None
        # Set-wide connectivity back-off (ISSUE_84) — one pause for the set instead of N
        # quarantines for N feeds.
        self._host_backoff_until: Optional[datetime] = None
        self._host_started_at: Optional[datetime] = None
        # The event the last resolved pass produced, waiting to be picked up by the worker.
        self._pass_event: Optional[HostEvent] = None
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
                # The rung of each of those, from the episode that set it — one query for the
                # whole fleet at boot instead of one per skip forever after.
                if self._quarantined:
                    cur.execute(
                        f'SELECT DISTINCT ON (source_id) source_id, rung, rungs_total '
                        f'FROM {self._EPISODES} WHERE kind = %s AND ended_at IS NULL '
                        'AND source_id = ANY(%s) ORDER BY source_id, started_at DESC',
                        (_QUARANTINE, sorted(self._quarantined)))
                    self._rungs = {sid: (rung, total) for sid, rung, total in cur.fetchall()
                                   if rung is not None}
                # A correlated event left open by a killed process would otherwise read as
                # "still running" forever. Close it at its last extension — the final moment the
                # engine actually observed the outage.
                #
                # Only *stale* ones: this store is also built for a pipeline runner's reach
                # check, and an event that a live ingest worker is still extending must survive
                # that. An event untouched for three back-off cycles cannot have a live writer.
                stale = timedelta(minutes=self._config.correlated_backoff_minutes * 3)
                cur.execute(
                    f'UPDATE {self._EPISODES} SET ended_at = updated_at, outcome = %s '
                    'WHERE kind = %s AND ended_at IS NULL AND updated_at < %s',
                    ('resumed', _CORRELATED, now - stale))
        except psycopg.Error as exc:
            raise VectorStoreError(f'source_health load failed: {exc}') from exc

    # ------------------------------------------------------------------ pass scope (ISSUE_84)

    @contextmanager
    def pass_scope(self, source_set: str) -> Iterator[None]:
        """Bracket one ingest pass, so the quarantine decision sees the whole pass.

        `record_failure` inside the scope writes its counters immediately but withholds the
        flag; leaving the scope either applies the withheld flags or — when the pass looks like
        a local connectivity failure — discards them and opens a set-wide event instead.

        Without a scope the store behaves exactly as before (flags apply immediately), which is
        what the manual CLI path and the tests that predate ISSUE_84 rely on.
        """
        self._pass = _PassState(source_set=source_set)
        try:
            yield
        finally:
            state, self._pass = self._pass, None
            if state is not None:
                self._resolve_pass(state)

    def _resolve_pass(self, state: _PassState) -> None:
        """Apply or discard the pass's withheld flags — the one place the policy is decided."""
        pollable = state.pollable
        failed = len(state.failed)
        ratio = failed / pollable if pollable else 0.0
        # A thin pass cannot look correlated: two feeds failing out of two due ones is not
        # evidence about the host, it is evidence about two feeds.
        correlated = (pollable >= self._config.correlated_min_pollable
                      and ratio >= self._config.correlated_failure_ratio)
        if correlated:
            self._enter_host_event(state, failed, pollable)
            return
        # Not correlated: an open event ends here — this pass proves connectivity is back.
        if self._host_started_at is not None and state.succeeded:
            self._leave_host_event(state)
        for pending in state.pending:
            self._apply_flag(pending)

    def _enter_host_event(self, state: _PassState, failed: int, pollable: int) -> None:
        """Record the set-wide failure, discard every withheld flag, arm the back-off."""
        now = datetime.now(timezone.utc)
        opening = self._host_started_at is None
        if opening:
            self._host_started_at = now
        self._host_backoff_until = now + timedelta(
            minutes=self._config.correlated_backoff_minutes)
        fleet = self._fleet_view(state.source_set, failed, pollable)
        try:
            with self._connect() as conn, conn.cursor() as cur:
                if opening:
                    cur.execute(
                        f'INSERT INTO {self._EPISODES} (kind, source_set, started_at, '
                        'trigger_type, failed_of, timeline, updated_at) '
                        'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                        (_CORRELATED, state.source_set, now, 'HOST_UNREACHABLE',
                         f'{failed}/{pollable}', json.dumps([{'fleet': fleet}]), now))
                else:
                    # One row per event, extended: the operator wants "how long did it last",
                    # not one row per five-minute retry.
                    cur.execute(
                        f'UPDATE {self._EPISODES} SET updated_at = %s, failed_of = %s '
                        'WHERE kind = %s AND source_set = %s AND ended_at IS NULL',
                        (now, f'{failed}/{pollable}', _CORRELATED, state.source_set))
        except psycopg.Error as exc:
            raise VectorStoreError(f'correlated event record failed: {exc}') from exc
        # The withheld flags die here: no quarantine, no rung advance, no episode. The streaks
        # stay — when connectivity returns partially, the feeds that are genuinely dead cross
        # the ratio back down and are flagged at once, carrying their large streak.
        state.pending.clear()
        self._pass_event = HostEvent(
            source_set=state.source_set, failed=failed, pollable=pollable,
            started_at=self._host_started_at, backoff_until=self._host_backoff_until,
            fleet=fleet, opened=opening)

    def _leave_host_event(self, state: _PassState) -> None:
        """Connectivity is back — close the event and let normal polling resume."""
        now = datetime.now(timezone.utc)
        started = self._host_started_at or now
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'UPDATE {self._EPISODES} SET ended_at = %s, outcome = %s, updated_at = %s '
                    'WHERE kind = %s AND source_set = %s AND ended_at IS NULL',
                    (now, 'resumed', now, _CORRELATED, state.source_set))
        except psycopg.Error as exc:
            raise VectorStoreError(f'correlated event close failed: {exc}') from exc
        self._pass_event = HostEvent(
            source_set=state.source_set, failed=len(state.failed), pollable=state.pollable,
            started_at=started, backoff_until=now, resumed=True,
            duration_seconds=(now - started).total_seconds())
        self._host_started_at = None
        self._host_backoff_until = None

    def _fleet_view(self, source_set: str, failed: int, pollable: int) -> str:
        """How the *other* sets are faring — the decision is per set, the wording is not.

        12/12 across two independently-configured sets says "the host"; 5/5 in one set while the
        other is healthy says "one upstream provider". Both suppress the quarantine, but they
        send the operator to different places. One small SELECT, only when an event opens.
        """
        parts = [f'{source_set} {failed}/{pollable}']
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT source_set, count(*) FILTER (WHERE consecutive_failures > 0), '
                    f'count(*) FROM {self._TABLE} WHERE source_set <> %s AND source_set <> %s '
                    'GROUP BY source_set ORDER BY source_set',
                    (source_set, ''))
                others = cur.fetchall()
        except psycopg.Error:
            # A label is not worth failing an outage response over.
            return parts[0]
        for other_set, other_failed, other_total in others:
            if other_failed:
                parts.append(f'{other_set} {other_failed}/{other_total}')
        if len(parts) == 1:
            return f'{parts[0]}, other sets healthy'
        return ' + '.join(parts)

    def take_host_event(self) -> Optional[HostEvent]:
        """The connectivity event this pass opened, continued or closed — consumed once.

        The store decides; the worker logs and alerts. Handing the event out instead of logging
        it here keeps consoles and alert channels out of a unit that only knows about policy.
        """
        event, self._pass_event = self._pass_event, None
        return event

    def host_backoff_until(self) -> Optional[datetime]:
        """When the set-wide back-off ends, or None when there is none."""
        if self._host_backoff_until and self._host_backoff_until > datetime.now(timezone.utc):
            return self._host_backoff_until
        return None

    # ------------------------------------------------------------------ the hot path

    def should_poll(self, source_id: str) -> bool:
        """False while the source is quarantined or the set is backing off (no DB hit).

        At cool-off expiry this returns True exactly once with the source marked as *probing*:
        the next recorded outcome then closes the episode or escalates it, instead of the feed
        quietly re-entering normal polling and needing five fresh failures to be caught again.
        """
        if self.host_backoff_until() is not None:
            return False
        until = self._quarantined.get(source_id)
        if until is None:
            return True
        if until > datetime.now(timezone.utc):
            return False
        # Cool-off elapsed — hand out the single half-open probe. The rung stays cached: the
        # probe's outcome is what decides whether it resets or climbs, not the expiry.
        self._quarantined.pop(source_id, None)
        self._probing.add(source_id)
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

    def rung_of(self, source_id: str) -> Optional[Tuple[int, int]]:
        """The ladder position a currently-quarantined source sits on, as (rung, total).

        Lets the surfaces say "1/3" instead of only "quarantined" — the difference between "wait
        an hour" and "this feed is effectively gone". Answered from memory: the ingestor asks on
        every skipped poll, which is the hot path `should_poll` keeps DB-free, and the value is
        already known wherever it is set (`_apply_flag`) or loaded (`_load_quarantines`).
        """
        return self._rungs.get(source_id)

    def record_success(self, source_id: str, host: str, source_set: str,
                       status: int = 200) -> bool:
        """Record a healthy poll. Clears any flag/quarantine (recovery). Returns True if the
        source had been flagged before — so the worker can log a one-line recovery notice."""
        now = datetime.now(timezone.utc)
        probing = source_id in self._probing
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
                    # Cleared with the streak: `last_status` describes THIS poll, so leaving the
                    # error type from an older one made healthy rows read 'UNREACHABLE / 200' —
                    # two different events rendered as one contradictory state. The failure
                    # history it carried lives in `recent_events` and the poll journal.
                    'last_error_type = NULL, '
                    'quarantined_until = NULL, updated_at = EXCLUDED.updated_at',
                    (source_id, host, source_set, now, status, now))
                # The probe answered: close the episode it belonged to. `probe_ok` is what the
                # history renders — it distinguishes "recovered on its own schedule" from a
                # cool-off that merely expired.
                if was_flagged or probing:
                    self._close_episode(cur, source_id, now, 'probe_ok' if probing else 'resumed')
        except psycopg.Error as exc:
            raise VectorStoreError(f'source_health success record failed: {exc}') from exc
        self._quarantined.pop(source_id, None)
        self._rungs.pop(source_id, None)
        self._probing.discard(source_id)
        if self._pass is not None:
            self._pass.succeeded.add(source_id)
        return was_flagged

    def record_failure(self, source_id: str, host: str, source_set: str, *,
                       error_type: str, status: Optional[int], message: str,
                       duration_ms: Optional[float] = None,
                       deadline_ms: Optional[float] = None) -> HealthOutcome:
        """Record a failed poll: bump counters, append a capped event, and decide the quarantine.

        Inside a `pass_scope` the decision is *withheld* until the pass ends (ISSUE_84) — the
        counters and the event ring are still written here and now, so a pass that dies mid-way
        loses no accounting. Outside a scope the flag applies immediately, as it always did.

        `duration_ms` against `deadline_ms` is what splits the overloaded `UNREACHABLE` bucket:
        a failure that burned the fetch deadline is a feed that went quiet, one that came back in
        milliseconds is a refusal. Same taxonomy, different cool-off.
        """
        now = datetime.now(timezone.utc)
        probing = source_id in self._probing
        event = {'ts': now.isoformat(), 'level': _level_for(error_type),
                 'type': error_type, 'status': status, 'message': message[:300]}
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT consecutive_failures, flagged, recent_events FROM {self._TABLE} '
                    'WHERE source_id = %s', (source_id,))
                row = cur.fetchone()
                consecutive = (row[0] if row else 0) + 1
                events: List[dict] = list(row[2]) if row and row[2] else []
                events.append(event)
                events = events[-self._config.recent_events_kept:]   # keep the last N (overview)

                # The flag columns are deliberately NOT touched here any more (ISSUE_84): this
                # write is accounting, the quarantine is a decision, and the two now happen at
                # different moments.
                cur.execute(
                    f'INSERT INTO {self._TABLE} (source_id, host, source_set, total_polls, '
                    'total_failures, consecutive_failures, last_failure_at, last_status, '
                    'last_error_type, recent_events, updated_at) '
                    'VALUES (%s, %s, %s, 1, 1, %s, %s, %s, %s, %s, %s) '
                    'ON CONFLICT (source_id) DO UPDATE SET '
                    'host = EXCLUDED.host, source_set = EXCLUDED.source_set, '
                    f'total_polls = {self._TABLE}.total_polls + 1, '
                    f'total_failures = {self._TABLE}.total_failures + 1, '
                    'consecutive_failures = EXCLUDED.consecutive_failures, '
                    'last_failure_at = EXCLUDED.last_failure_at, last_status = EXCLUDED.last_status, '
                    'last_error_type = EXCLUDED.last_error_type, '
                    'recent_events = EXCLUDED.recent_events, updated_at = EXCLUDED.updated_at',
                    (source_id, host, source_set, consecutive, now, status, error_type,
                     json.dumps(events), now))
        except psycopg.Error as exc:
            raise VectorStoreError(f'source_health failure record failed: {exc}') from exc

        # A failed probe escalates on the spot — it already served its five-failure sentence.
        crossed = consecutive >= self._config.flag_after_consecutive_failures
        if not (crossed or probing):
            if self._pass is not None:
                self._pass.failed.add(source_id)
            return HealthOutcome(consecutive, False, None)

        # Provisional until the pass resolves: the correlated guard may still rule this a local
        # connectivity failure and drop it. `_apply_flag` completes this same object.
        outcome = HealthOutcome(consecutive, False, None, probe=probing, suppressed=True)
        pending = _PendingFlag(source_id=source_id, host=host, source_set=source_set,
                               error_type=error_type, status=status, duration_ms=duration_ms,
                               deadline_ms=deadline_ms, streak=consecutive, probe=probing,
                               events=events, outcome=outcome)
        if self._pass is not None:
            self._pass.failed.add(source_id)
            self._pass.pending.append(pending)
            return outcome
        return self._apply_flag(pending)

    # ------------------------------------------------------------------ the decision

    def _apply_flag(self, pending: _PendingFlag) -> HealthOutcome:
        """Quarantine one source: resolve its rung, write the state, open the episode."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=self._config.ladder_reset_hours)
        last = len(self._ladder) - 1
        try:
            with self._connect() as conn, conn.cursor() as cur:
                # The ladder's memory: how many episodes this feed has had inside the reset
                # window. Counted from the history that also explains it — never a stored
                # counter, which would be a second truth free to drift from the rows.
                cur.execute(
                    f'SELECT count(*) FROM {self._EPISODES} WHERE kind = %s AND source_id = %s '
                    'AND started_at > %s', (_QUARANTINE, pending.source_id, since))
                episodes = int(cur.fetchone()[0])
                # History can only make it worse, never better: a 403 lands on the top rung on
                # its first episode, and a feed that keeps relapsing climbs regardless of type.
                start = _start_rung(pending.error_type, pending.status, pending.duration_ms,
                                    pending.deadline_ms, self._config.deadline_ratio, last)
                rung = min(max(episodes, start), last)
                hours = self._ladder[rung]
                until = now + timedelta(hours=hours)
                # A failed probe closes the episode it belonged to before the next one opens,
                # so the history reads as a chain rather than a set of overlapping rows.
                if pending.probe:
                    self._close_episode(cur, pending.source_id, now, 'escalated')
                cur.execute(
                    f'UPDATE {self._TABLE} SET flagged = TRUE, '
                    f'flagged_at = COALESCE({self._TABLE}.flagged_at, %s), '
                    'quarantined_until = %s, updated_at = %s WHERE source_id = %s',
                    (now, until, now, pending.source_id))
                cur.execute(
                    f'INSERT INTO {self._EPISODES} (kind, source_id, source_set, started_at, '
                    'rung, rungs_total, cooloff_hours, trigger_type, trigger_status, trigger_ms, '
                    'streak, timeline, updated_at) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    (_QUARANTINE, pending.source_id, pending.source_set, now, rung, last + 1,
                     float(hours), pending.error_type, pending.status, pending.duration_ms,
                     pending.streak, json.dumps(pending.events), now))
        except psycopg.Error as exc:
            raise VectorStoreError(f'source_health quarantine failed: {exc}') from exc
        self._quarantined[pending.source_id] = until
        self._rungs[pending.source_id] = (rung, last + 1)
        self._probing.discard(pending.source_id)
        # Completed in place — see `_PendingFlag.outcome`.
        outcome = pending.outcome
        outcome.just_flagged = True
        outcome.quarantined_until = until
        outcome.rung, outcome.rungs_total = rung, last + 1
        outcome.suppressed = False
        return outcome

    def _close_episode(self, cur: psycopg.Cursor, source_id: str, ended_at: datetime,
                       outcome: str) -> None:
        """Close this source's open quarantine episode, if it has one."""
        cur.execute(
            f'UPDATE {self._EPISODES} SET ended_at = %s, outcome = %s, updated_at = %s '
            'WHERE kind = %s AND source_id = %s AND ended_at IS NULL',
            (ended_at, outcome, ended_at, _QUARANTINE, source_id))


def _start_rung(error_type: str, status: Optional[int], duration_ms: Optional[float],
                deadline_ms: Optional[float], ratio: float, last: int) -> int:
    """Which rung a failure *starts* on, from its type and how long it took (ISSUE_84).

    The taxonomy is deliberately not split for this: `error_type` is stamped into
    `source_health.last_error_type` and every `source_poll_log` row, so a new `DNS_ERROR` would
    cut the existing series for a distinction the duration already carries — DNS and refusals
    return in milliseconds, a feed that went quiet burns the whole deadline. Three orders of
    magnitude apart, which is why the cut point is not delicate.

    Without a measured duration (an older call site, a source that cannot time itself) the
    conservative reading wins: treat it as transient, since over-quarantining is the defect this
    issue exists to fix.
    """
    if error_type == 'RATE_LIMITED':
        return min(1, last)                     # alive and talking, we are just too fast
    if error_type == 'PARSE_ERROR':
        return last                             # a broken body will not fix itself soon
    if error_type == 'HTTP_ERROR':
        # 5xx is their outage (usually short); 4xx is a refusal aimed at us (durable).
        return 0 if status is not None and status >= 500 else last
    # UNREACHABLE: the bucket the duration has to split.
    if duration_ms is None or deadline_ms is None:
        return 0
    return 0 if duration_ms >= ratio * deadline_ms else last


def _level_for(error_type: str) -> str:
    """Map an error type to a warn/error level for the recent-events overview.

    Transient / external throttling is a warning (we back off and retry); a broken body or a
    hard HTTP status is an error (the feed itself is wrong)."""
    return 'warning' if error_type in ('RATE_LIMITED', 'UNREACHABLE') else 'error'
