"""Quarantine history report (ISSUE_84) — what the policy did to a feed, and what it cost.

The read side of `source_quarantine_log`. `source_health_report.py` answers *"how is this feed
right now"* and `source_latency_report.py` answers *"how does it behave"*; neither can answer the
question the 2026-08 incidents kept raising: **"this feed keeps dropping out — show me the history,
and tell me whether our own reaction was proportionate."**

Two views, because they answer different questions at different scales:

- the **episode list** — every quarantine and every connectivity event for a feed, with the rung
  each one reached, so a recurrence is judged from a record rather than an impression. The
  recurrence *rate* is the forward-looking number: two episodes 41 hours apart are noise, two
  ninety minutes apart are a feed on its way out — same count, opposite diagnosis;
- the **episode view** — the poll-by-poll run-up to one decision, with the decision printed next
  to the evidence that produced it (including why the correlated guard did *not* fire).

Both print absolute UTC stamps rather than relative ages, so a line here joins directly against
`cost_log`, the outcome archive and the rotating file log. The 2026-08 investigations each needed
five ad-hoc queries and a `py-spy` session; this exists so the next one needs a command.

Correlated events (`kind='correlated'`) appear in a feed's list even though they belong to the
whole set: they explain a *gap* in its history. Without them 2026-07-29 reads as "failed five
times, nothing happened", and nobody can tell whether the policy worked or was suppressed.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# What the pre-ISSUE_84 policy would have charged for any episode — the yardstick that makes the
# change measurable instead of merely asserted.
_OLD_FLAT_HOURS = 24.0


@dataclass
class QuarantineEpisodeRow:
    """One decision in a feed's history: a quarantine, or a connectivity event that spared it."""
    kind: str                               # quarantine | correlated
    source_set: str
    started_at: datetime
    ended_at: Optional[datetime]
    rung: Optional[int]                     # 0-based; None for a correlated event
    rungs_total: Optional[int]
    cooloff_hours: Optional[float]
    trigger_type: Optional[str]
    trigger_status: Optional[int]
    trigger_ms: Optional[float]
    streak: Optional[int]
    failed_of: Optional[str]                # '12/12' on a correlated event
    outcome: Optional[str]                  # probe_ok | escalated | resumed | manual_clear
    timeline: List[dict] = field(default_factory=list)
    cadence_seconds: Optional[float] = None   # the feed's own poll interval — prices the outage

    @property
    def correlated(self) -> bool:
        return self.kind == 'correlated'

    @property
    def duration_seconds(self) -> float:
        """How long the feed was actually out of the loop — running episodes count up to now."""
        end = self.ended_at or datetime.now(timezone.utc)
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def polls_missed(self) -> Optional[int]:
        """Polls this episode cost, in the feed's own cadence — the unit the argument turns on."""
        if not self.cadence_seconds:
            return None
        return int(self.duration_seconds / self.cadence_seconds)

    @property
    def polls_missed_under_old_policy(self) -> Optional[int]:
        """What the flat 24h would have cost — only meaningful for a quarantine we shortened."""
        if self.correlated or not self.cadence_seconds:
            return None
        return int(_OLD_FLAT_HOURS * 3600.0 / self.cadence_seconds)


@dataclass
class SourceQuarantineReport:
    """One feed's episode history plus the state the ladder is currently in."""
    source_id: str
    since_label: str
    rows: List[QuarantineEpisodeRow] = field(default_factory=list)
    cadence_seconds: Optional[float] = None
    current_rung: Optional[int] = None          # from the open episode, if the feed is in one
    current_rungs_total: Optional[int] = None
    ladder_resets_at: Optional[datetime] = None  # when the most recent episode ages out
    history_missing: bool = False               # the table does not exist yet (pre-migration 007)

    @property
    def quarantines(self) -> List[QuarantineEpisodeRow]:
        return [row for row in self.rows if not row.correlated]

    @property
    def correlated_events(self) -> List[QuarantineEpisodeRow]:
        return [row for row in self.rows if row.correlated]

    @property
    def missed_to_policy(self) -> int:
        """Polls lost because *we* stopped polling — the self-inflicted half."""
        return sum(row.polls_missed or 0 for row in self.quarantines)

    @property
    def missed_to_outage(self) -> int:
        """Polls lost to a connectivity failure — the half that was never ours to prevent."""
        return sum(row.polls_missed or 0 for row in self.correlated_events)

    @property
    def escalations_to_max(self) -> int:
        return sum(1 for row in self.quarantines
                   if row.rung is not None and row.rungs_total
                   and row.rung == row.rungs_total - 1)


def build_source_quarantine_report(database_url: str, source_id: str, since: datetime, *,
                                   since_label: str = '30d',
                                   ladder_reset_hours: int = 168,
                                   table: str = 'source_quarantine_log',
                                   journal_table: str = 'source_poll_log'
                                   ) -> SourceQuarantineReport:
    """Read one feed's episodes in the window, priced against its own poll cadence."""
    report = SourceQuarantineReport(source_id=source_id, since_label=since_label)
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # A database from before migration 007 is a valid, empty answer — not a crash. The
            # read side never creates or migrates; that is the migration runner's job alone.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (table,))
            if cur.fetchone()[0] == 0:
                report.history_missing = True
                return report
            cadence = _cadence_seconds(cur, source_id, since, journal_table)
            report.cadence_seconds = cadence
            # A feed's own quarantines, plus the connectivity events of the set it belongs to —
            # the events are what explain the quarantines that did NOT happen.
            cur.execute(
                'SELECT kind, source_set, started_at, ended_at, rung, rungs_total, cooloff_hours, '
                'trigger_type, trigger_status, trigger_ms, streak, failed_of, outcome, timeline '
                f'FROM {table} WHERE started_at >= %s AND (source_id = %s OR (kind = %s '
                f'AND source_set = (SELECT source_set FROM {table} WHERE source_id = %s '
                'ORDER BY started_at DESC LIMIT 1))) ORDER BY started_at',
                (since, source_id, 'correlated', source_id))
            for row in cur.fetchall():
                report.rows.append(QuarantineEpisodeRow(
                    kind=row[0], source_set=row[1], started_at=row[2], ended_at=row[3],
                    rung=row[4], rungs_total=row[5], cooloff_hours=row[6], trigger_type=row[7],
                    trigger_status=row[8], trigger_ms=row[9], streak=row[10], failed_of=row[11],
                    outcome=row[12], timeline=list(row[13] or []), cadence_seconds=cadence))
    except psycopg.Error as exc:
        raise VectorStoreError(f'quarantine history report failed: {exc}') from exc

    quarantines = report.quarantines
    if quarantines:
        newest = quarantines[-1]
        # The ladder's memory is a window since the LAST episode, so the reset date is what the
        # operator needs to predict the next cool-off — not the count on its own.
        report.ladder_resets_at = newest.started_at + timedelta(hours=ladder_reset_hours)
        report.current_rung, report.current_rungs_total = newest.rung, newest.rungs_total
    return report


def build_quarantine_episode(database_url: str, source_id: str, started_at: datetime, *,
                             table: str = 'source_quarantine_log',
                             journal_table: str = 'source_poll_log'
                             ) -> Optional[QuarantineEpisodeRow]:
    """One episode with the poll-by-poll run-up that produced it.

    The timeline prefers the journal (full resolution, every poll) and falls back to the copy
    frozen into the episode at decision time. That fallback is the reason the copy exists: the
    journal keeps 14 days, an episode series is read for months, and the minutes that triggered a
    decision are exactly the ones worth outliving the retention.
    """
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (table,))
            if cur.fetchone()[0] == 0:
                return None
            cur.execute(
                'SELECT kind, source_set, started_at, ended_at, rung, rungs_total, cooloff_hours, '
                'trigger_type, trigger_status, trigger_ms, streak, failed_of, outcome, timeline '
                f'FROM {table} WHERE source_id = %s AND started_at = %s',
                (source_id, started_at))
            row = cur.fetchone()
            if row is None:
                return None
            episode = QuarantineEpisodeRow(
                kind=row[0], source_set=row[1], started_at=row[2], ended_at=row[3], rung=row[4],
                rungs_total=row[5], cooloff_hours=row[6], trigger_type=row[7],
                trigger_status=row[8], trigger_ms=row[9], streak=row[10], failed_of=row[11],
                outcome=row[12], timeline=list(row[13] or []))
            episode.cadence_seconds = _cadence_seconds(
                cur, source_id, episode.started_at - timedelta(days=1), journal_table)
            # The run-up: the last polls before the decision, plus whatever resolved it.
            cur.execute(
                f'SELECT ts, outcome, duration_ms, error_type, status FROM {journal_table} '
                'WHERE source_id = %s AND ts BETWEEN %s AND %s ORDER BY ts',
                (source_id, episode.started_at - timedelta(minutes=15),
                 (episode.ended_at or datetime.now(timezone.utc)) + timedelta(minutes=1)))
            journal = [{'ts': ts.isoformat(), 'outcome': outcome, 'duration_ms': duration,
                        'type': error_type, 'status': status}
                       for ts, outcome, duration, error_type, status in cur.fetchall()]
            if journal:
                episode.timeline = journal
    except psycopg.Error as exc:
        raise VectorStoreError(f'quarantine episode read failed: {exc}') from exc
    return episode


def _cadence_seconds(cur: psycopg.Cursor, source_id: str, since: datetime,
                     journal_table: str) -> Optional[float]:
    """This feed's own median seconds between polls — what an outage is priced in.

    The same idea `source_latency_report` uses to *find* outages, narrowed to one source because
    here the outage is already known and only needs a price. Deliberately not shared with that
    report's gap query: that one computes cadence for every source as part of a window function
    over the whole journal, and pulling one number out of it would mean running all of it.
    """
    cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                (journal_table,))
    if cur.fetchone()[0] == 0:
        return None
    cur.execute(
        'WITH gaps AS (SELECT EXTRACT(EPOCH FROM (ts - lag(ts) OVER (ORDER BY ts))) AS gap_s '
        f'FROM {journal_table} WHERE source_id = %s AND ts >= %s) '
        'SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_s) FROM gaps',
        (source_id, since))
    row = cur.fetchone()
    return float(row[0]) if row and row[0] else None


def _stamp(moment: Optional[datetime]) -> str:
    return moment.strftime('%Y-%m-%d %H:%M:%S') if moment else '—'


def _hours(value: Optional[float]) -> str:
    if not value:
        return '—'
    if value < 1:
        return f'{int(value * 60)}m'
    return f'{value:g}h'


def _duration(seconds: float) -> str:
    """`3m42s` · `1h00m03s` — precise, because "about an hour" is what the argument is about."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours}h{minutes:02d}m{secs:02d}s'
    return f'{minutes}m{secs:02d}s' if minutes else f'{secs}s'


def _trigger_cell(row: QuarantineEpisodeRow) -> str:
    """What set this off, with the measurement that picked the rung."""
    if row.correlated:
        return f'⚠ host {row.failed_of or "?"}'
    parts = [row.trigger_type or '?']
    if row.trigger_status is not None:
        parts.append(str(row.trigger_status))
    if row.trigger_ms is not None:
        # `(dl)` = it burned the fetch deadline, which is why it got the SHORT rung. Without the
        # duration this column could not explain why two UNREACHABLE failures were treated
        # differently — which is the single most confusing thing about the policy.
        parts.append(f'{row.trigger_ms / 1000.0:.1f}s')
    return ' '.join(parts)


def format_source_quarantine_report(report: SourceQuarantineReport) -> str:
    """Render the episode list as the shared console pattern (title + window + dividers)."""
    divider = '-' * 96
    set_name = report.rows[0].source_set if report.rows else ''
    title = f'quarantine history — {report.source_id} ({report.since_label}'
    lines = [
        title + (f', {set_name})' if set_name else ')'),
        divider,
        f'{"started (UTC)":19}  {"rung":5} {"cool-off":8} {"trigger":26} {"ended (UTC)":19}  '
        f'{"outcome":9} {"missed":>6}',
        divider,
    ]
    if report.history_missing:
        lines.append('(no quarantine history yet — apply migration 007 and let the workers run)')
        return '\n'.join(lines)
    for row in report.rows:
        rung = (f'{row.rung + 1}/{row.rungs_total}'
                if row.rung is not None and row.rungs_total else '—')
        missed = row.polls_missed
        lines.append(
            f'{_stamp(row.started_at):19}  {rung:5} {_hours(row.cooloff_hours):8} '
            f'{_trigger_cell(row):26.26} {_stamp(row.ended_at):19}  '
            f'{(row.outcome or "running"):9} {(missed if missed is not None else "—"):>6}')
        if row.correlated:
            # The second line is the point of the row: it records what did NOT happen.
            lines.append(f'{"":19}  {"":5} {"":8} no quarantine, no rung advance')
    if not report.rows:
        lines.append('(no quarantine episodes in the window — the feed was never held back)')
        return '\n'.join(lines + [divider])
    lines.append(divider)
    lines.append(
        f'{len(report.rows)} events ({len(report.quarantines)} quarantines, '
        f'{len(report.correlated_events)} host) · '
        + _ladder_state(report)
        + f' · escalations to max: {report.escalations_to_max}')
    # The batch's acceptance metric: what our own policy cost, kept apart from what the outside
    # world cost. Before ISSUE_84 both were the same undifferentiated "the feed was gone".
    lines.append(f'polls missed:  {report.missed_to_policy} to policy · '
                 f'{report.missed_to_outage} to the outage')
    if report.cadence_seconds:
        lines.append(f'(priced at this feed\'s own cadence: one poll every '
                     f'{report.cadence_seconds:.0f}s)')
    return '\n'.join(lines)


def _ladder_state(report: SourceQuarantineReport) -> str:
    if report.current_rung is None or not report.current_rungs_total:
        return 'rung —'
    return (f'rung now {report.current_rung + 1}/{report.current_rungs_total}, '
            f'resets {_stamp(report.ladder_resets_at)}')


def format_quarantine_episode(episode: QuarantineEpisodeRow, source_id: str) -> str:
    """Render one episode's run-up: every poll, the decision, and what resolved it."""
    divider = '-' * 96
    rung = (f'rung {episode.rung + 1}/{episode.rungs_total}'
            if episode.rung is not None and episode.rungs_total else 'connectivity event')
    lines = [
        f'episode {_stamp(episode.started_at)} UTC — {source_id} — {rung}'
        + (f' ({_hours(episode.cooloff_hours)})' if episode.cooloff_hours else ''),
        divider,
    ]
    for entry in episode.timeline:
        lines.append(_timeline_line(entry))
    if not episode.timeline:
        lines.append('(no poll detail — the journal window has passed and no snapshot was frozen)')
    lines.append(divider)
    summary = [f'held {_duration(episode.duration_seconds)}']
    if episode.streak:
        summary.append(f'{episode.streak} consecutive failures')
    missed = episode.polls_missed
    if missed is not None:
        summary.append(f'{missed} polls missed')
    old = episode.polls_missed_under_old_policy
    if old is not None and missed is not None and old > missed:
        # Kept deliberately: it is the only line that shows the policy change *working* rather
        # than being asserted. Worth dropping once the ladder is no longer new.
        summary.append(f'under the old flat policy: {old}')
    lines.append(' · '.join(summary))
    return '\n'.join(lines)


def _timeline_line(entry: dict) -> str:
    """One poll, or one frozen health event — the two shapes the timeline can carry."""
    stamp = str(entry.get('ts', ''))[11:19]
    # Journal shape (full resolution) vs the frozen `recent_events` shape (failures only).
    if 'outcome' in entry:
        duration = entry.get('duration_ms')
        took = f'{duration:>8.0f}ms' if duration is not None else ' ' * 10
        if entry.get('outcome') == 'ok':
            return f'{stamp}  {"ok":12}{took}'
        status = f' {entry["status"]}' if entry.get('status') is not None else ''
        return f'{stamp}  {(entry.get("type") or "failed"):12}{took}{status}'
    if 'fleet' in entry:
        return f'{stamp}  correlated   {entry["fleet"]}'
    status = f' ({entry["status"]})' if entry.get('status') is not None else ''
    return f'{stamp}  {(entry.get("type") or "?"):12}{status}  {entry.get("message", "")}'
