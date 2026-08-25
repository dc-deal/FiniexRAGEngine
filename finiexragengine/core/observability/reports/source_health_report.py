"""Source-health report (ISSUE_11) — feed reliability + the debugging-ready problem log.

Reads the `source_health` rows the ingest worker captured (CLAUDE.md — report from the store)
and renders the shared console pattern: per-feed poll counts / success rate / flag+quarantine
state, a capped list of the most recent warnings/errors (so the operator debugs a feed without
digging through logs), and an **orphan notice** for sources still in the store but no longer in
any current config (`may be deleted`). The same aggregation feeds the weekly report (#27).

**The silence rule (ISSUE_107) — reliability is not delivery.** Every number above measures the
*poll*: a feed can answer 200 on all 102,136 of them and have put nothing in the corpus for a
month. That happened to be invisible here, because the health store has no idea what an article
is. So the report joins the corpus: newest stored article and how many arrived in the silence
window, per feed. A feed polling successfully with **zero** stored articles over the window is
`SILENT` — threshold-free on purpose (no age policy to get wrong), and it is what catches the
"endpoint answers, feed is a fossil" shape that `feed_doctor`'s staleness gate catches live.

Two honestly different questions, deliberately kept apart: `feed_doctor` probes the feed *now*
and needs the network; this reads what the feed *delivered* and needs only the store.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# The debugging-ready problem log is capped for overview (operator: "max 10"). A default rather than
# a constant since ISSUE_104: `reports.source_health.recent_problems` sets it, and a call may raise
# it for one look without editing config. It caps the CONSOLE only — the payload carries what the
# store holds.
_RECENT_PROBLEMS = 10

# The silence rule's span, mirroring `reports.source_health.silence_days`. A verdict threshold, so
# it is config-only — never a per-call parameter (see `report_config_types`).
_SILENCE_DAYS = 7


@dataclass
class SourceHealthRow:
    """One feed's rolling health, as read from source_health."""
    source_id: str
    host: str
    source_set: str
    total_polls: int
    total_success: int
    total_failures: int
    consecutive_failures: int
    last_success_at: Optional[datetime]
    last_failure_at: Optional[datetime]
    last_status: Optional[int]
    last_error_type: Optional[str]
    flagged: bool
    quarantined_until: Optional[datetime]
    recent_events: List[dict] = field(default_factory=list)
    # Config state, not health state — the store has no column for it, because whether a feed is
    # switched off says nothing about how it behaved when it was polled. It has to be marked all
    # the same: without it a disabled feed's last poll keeps reading `ok` forever, which is stale
    # history dressed as a live verdict.
    disabled: bool = False
    # What the feed actually delivered (ISSUE_107), joined from the article corpus rather than the
    # health store — `articles.source_id` is the same config id, which is why the join exists at
    # all. `newest_article_at` is lifetime; `contributed` is counted over the silence window.
    newest_article_at: Optional[datetime] = None
    # When this feed last put something in the corpus — the delivery clock the silence rule runs
    # on. Taken over `fetched_at`, not `published_at`: a feed re-publishing an old story still
    # delivered today, and dating the arrival by the article's own timestamp would call that
    # silence.
    last_delivery_at: Optional[datetime] = None
    contributed: int = 0
    silence_days: int = _SILENCE_DAYS
    # How long this feed is allowed to stay quiet, and where that number came from. Same
    # declaration the live probe judges staleness against (`SourceConfig.expected_max_age_hours`),
    # deliberately: "this feed is allowed to be quiet this long" is one fact, and letting the two
    # surfaces hold different numbers for it is how a healthy central bank ends up flagged on one
    # of them. Default = `silence_days`.
    allowance_hours: int = _SILENCE_DAYS * 24
    allowance_basis: str = 'default'
    # False when the corpus could not be read at all. Then `contributed = 0` means "not measured",
    # not "delivered nothing" — and a verdict must never be built on that difference being missed:
    # on a fresh database every polling feed would otherwise read SILENT at once.
    contribution_known: bool = True

    @property
    def success_rate(self) -> Optional[float]:
        return self.total_success / self.total_polls if self.total_polls else None

    @property
    def quarantined(self) -> bool:
        return bool(self.quarantined_until
                    and self.quarantined_until > datetime.now(timezone.utc))

    @property
    def silent(self) -> bool:
        """Polls fine, delivers nothing — the failure mode every other number here misses.

        Deliberately narrow, and narrow in three separate ways learned by running it:

        - A disabled feed is not silent (it is not polled at all), and neither is a quarantined or
          currently-failing one — those already have a verdict that explains them, and stacking a
          second on top would report one fault twice and bury the cause.
        - The window is the feed's **own** allowance, not a global one. Measured 2026-08-25: a flat
          7 days called `boc_press` (25 days between press releases) and `boe_news` (14) silent
          while both were perfectly healthy. A low-volume primary source is the point of those
          feeds, not a fault in them.
        - Nothing delivered *ever* counts as silence too — that is the "endpoint answers, feed is
          a fossil" case, and it must not hide behind a null.
        """
        if not self.contribution_known:
            return False
        if self.disabled or self.quarantined or self.consecutive_failures:
            return False
        if not self.total_success:
            return False
        if self.last_delivery_at is None:
            return True
        quiet_hours = (datetime.now(timezone.utc) - self.last_delivery_at).total_seconds() / 3600
        return quiet_hours > self.allowance_hours


@dataclass
class SourceHealthReport:
    rows: List[SourceHealthRow]
    orphans: List[str]        # source_ids in the store but not in any current config
    silence_days: int = _SILENCE_DAYS
    # False when the corpus could not be read (no `articles` table yet on a fresh database). The
    # silence rule is then not "nothing is silent" but "the rule did not run", and the report says
    # so — a barrier that cannot distinguish those two is a barrier nobody can trust.
    contribution_known: bool = True

    @property
    def flagged_count(self) -> int:
        return sum(1 for row in self.rows if row.flagged)

    @property
    def disabled_count(self) -> int:
        return sum(1 for row in self.rows if row.disabled)

    @property
    def silent_count(self) -> int:
        return sum(1 for row in self.rows if row.silent)


def _contributions(cur: psycopg.Cursor, since: datetime
                   ) -> Optional[Dict[str, Tuple[Optional[datetime], Optional[datetime], int]]]:
    """Per-source `(newest published, last delivery, rows in the window)`.

    Returns None when the corpus table does not exist — a fresh database, where the honest answer
    is "not measured" rather than "everything is silent".
    """
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'articles'")
    if cur.fetchone()[0] == 0:
        return None
    # One pass over the corpus. Two different clocks on purpose: `published_at` is what the FEED
    # claims (the same thing the live probe reads), `fetched_at` is when it actually reached us —
    # and the silence verdict runs on the second, because a feed re-publishing an old story still
    # delivered today. The count is the display number, over the shared window.
    cur.execute('SELECT source_id, max(published_at), max(fetched_at), '
                'count(*) FILTER (WHERE fetched_at >= %s) FROM articles GROUP BY source_id',
                (since,))
    return {source_id: (newest, last, count)
            for source_id, newest, last, count in cur.fetchall()}


def build_source_health_report(database_url: str, configured_ids: Set[str], *,
                               disabled_ids: Optional[Set[str]] = None,
                               table: str = 'source_health',
                               silence_days: int = _SILENCE_DAYS,
                               allowances: Optional[Dict[str, int]] = None
                               ) -> SourceHealthReport:
    """Load the health rows, mark orphans against the currently-configured source ids, and mark
    the switched-off ones.

    `disabled_ids` is a config fact the store cannot answer (there is no column for it) — an
    unmarked row would present a disabled feed's frozen last poll as a current `ok`.
    """
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (table,))
            if cur.fetchone()[0] == 0:
                return SourceHealthReport([], [], silence_days=silence_days)
            cur.execute(
                f'SELECT source_id, host, source_set, total_polls, total_success, '
                'total_failures, consecutive_failures, last_success_at, last_failure_at, '
                'last_status, last_error_type, flagged, quarantined_until, recent_events '
                f'FROM {table} ORDER BY source_id')
            disabled = disabled_ids or set()
            health = cur.fetchall()
            since = datetime.now(timezone.utc) - timedelta(days=silence_days)
            delivered = _contributions(cur, since)
            rows = []
            declared = allowances or {}
            for row in health:
                newest, last, contributed = (delivered or {}).get(row[0], (None, None, 0))
                # The feed's own allowance where it declares one, else the shared window. Same
                # declaration the live probe uses, so the two surfaces cannot disagree about how
                # long this feed is allowed to be quiet.
                own = declared.get(row[0])
                rows.append(SourceHealthRow(
                    *row[:13], recent_events=list(row[13] or []),
                    disabled=row[0] in disabled,
                    newest_article_at=newest, last_delivery_at=last, contributed=contributed,
                    silence_days=silence_days,
                    allowance_hours=own if own is not None else silence_days * 24,
                    allowance_basis='declared' if own is not None else 'default',
                    contribution_known=delivered is not None))
    except psycopg.Error as exc:
        raise VectorStoreError(f'source health report failed: {exc}') from exc

    orphans = sorted(row.source_id for row in rows if row.source_id not in configured_ids)
    return SourceHealthReport(rows, orphans, silence_days=silence_days,
                              contribution_known=delivered is not None)


def _ago(moment: Optional[datetime]) -> str:
    """Compact age of a timestamp ('12s', '3h', '2d', or '—')."""
    if moment is None:
        return '—'
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 90:
        return f'{seconds:.0f}s'
    if seconds < 5400:
        return f'{seconds / 60:.0f}m'
    if seconds < 172800:
        return f'{seconds / 3600:.0f}h'
    return f'{seconds / 86400:.0f}d'


def _remaining(moment: Optional[datetime]) -> str:
    """Compact time left until a future moment ('21h', '35m', or '0m')."""
    if moment is None:
        return '0m'
    minutes = max(0.0, (moment - datetime.now(timezone.utc)).total_seconds()) / 60
    return f'{minutes / 60:.0f}h' if minutes >= 90 else f'{minutes:.0f}m'


def _status_cell(row: SourceHealthRow) -> str:
    # `[disabled]` is appended, never substituted — same marker the feed doctor uses. The health
    # verdict stays visible on purpose: it is the record of how the feed behaved while it *was*
    # polled, and that is what the operator weighs when deciding to switch it back on.
    marker = ' [disabled]' if row.disabled else ''
    if row.flagged:
        detail = row.last_error_type or 'error'
        if row.quarantined:
            return (f'FLAGGED({detail}) quarantined '
                    f'{_remaining(row.quarantined_until)} left{marker}')
        # Cool-off elapsed. Every other verdict here describes the *last* poll; this one alone is
        # a claim about the *next* one — and a disabled feed has no next poll, so its row would
        # freeze reading "retrying" forever while nothing ever retries. Say what actually happens.
        verb = 'not polled' if row.disabled else 'retrying'
        return f'FLAGGED({detail}) {verb}{marker}'
    if row.consecutive_failures:
        return f'failing ({row.last_error_type or "error"}){marker}'
    # Only reached when nothing else explains the feed: the poll works and the corpus is empty
    # (ISSUE_107). The verdict carries its own basis — the count and the window it was taken over.
    if row.silent:
        if row.last_delivery_at is None:
            return f'SILENT (never delivered){marker}'
        # The verdict carries its own basis: how long quiet, against which allowance, set by whom.
        return (f'SILENT (quiet {_ago(row.last_delivery_at)} > {row.allowance_hours}h · '
                f'{row.allowance_basis}){marker}')
    return f'ok{marker}'


def _recent_problems(rows: Sequence[SourceHealthRow],
                     recent_problems: int = _RECENT_PROBLEMS) -> List[str]:
    """Newest warnings/errors across all feeds, capped for overview."""
    events = []
    for row in rows:
        for event in row.recent_events:
            events.append((event.get('ts', ''), row.source_id, event))
    events.sort(key=lambda item: item[0], reverse=True)
    lines = []
    for ts, source_id, event in events[:recent_problems]:
        when = ts.replace('T', ' ')[5:16] if ts else '—'          # MM-DD HH:MM
        status = f"({event.get('status')})" if event.get('status') is not None else ''
        lines.append(f"  [{source_id}] {when} {event.get('level', '?')} "
                     f"{event.get('type', '?')}{status}: {event.get('message', '')}")
    return lines


def format_source_health_report(report: SourceHealthReport,
                                recent_problems: int = _RECENT_PROBLEMS) -> str:
    """Render the report as the shared console pattern (title + window line + dividers)."""
    divider = '-' * 100
    quarantined = sum(1 for row in report.rows if row.quarantined)
    lines = [
        'Source Health — feeds, delivery & problems',
        f'sources: {len(report.rows)} tracked · {report.disabled_count} disabled · '
        f'{report.flagged_count} flagged · {quarantined} quarantined · '
        f'{report.silent_count} silent · {len(report.orphans)} orphaned',
        divider,
        f'{"source":18} {"host":20} {"polls":>7} {"ok%":>5} {"consec":>6} '
        f'{"last ok":>8} {"gave at":>7} {"gave":>6}  status',
        divider,
    ]
    for row in report.rows:
        rate = f'{row.success_rate * 100:.0f}%' if row.success_rate is not None else '—'
        consec = f'{row.consecutive_failures}' + ('!' if row.flagged else '')
        # `gave` is the delivery half: articles stored in the silence window. `?` where the corpus
        # could not be read — never 0, which would read as a verdict.
        gave = f'{row.contributed}' if row.contribution_known else '?'
        lines.append(f'{row.source_id:18.18} {row.host:20.20} {row.total_polls:>7} '
                     f'{rate:>5} {consec:>6} {_ago(row.last_success_at):>8} '
                     f'{_ago(row.last_delivery_at):>7} {gave:>6}  {_status_cell(row)}')
    if not report.rows:
        lines.append('(no source health captured yet — run the ingest workers)')
    lines.append(divider)
    # Where the delivery verdict came from — a barrier whose rule is invisible is a barrier nobody
    # can check, and "nothing was reported" has to be distinguishable from "nothing was measured".
    if report.contribution_known:
        declared = [row for row in report.rows if row.allowance_basis == 'declared']
        rule = (f'silence rule: nothing stored for longer than the feed is allowed to be quiet '
                f'· {report.silence_days}d default (reports.source_health.silence_days)')
        if declared:
            own = ', '.join(f'{row.source_id} {row.allowance_hours}h' for row in declared)
            rule += f' · {len(declared)} feed(s) declare their own ({own})'
        lines.append(rule)
        if report.silent_count:
            lines.append('SILENT: the poll succeeds and the corpus stays empty — the feed is '
                         'reachable and delivering nothing this engine can retrieve')
    else:
        lines.append('silence rule: NOT APPLIED — no article corpus to read '
                     '(the `gave` column is unmeasured, not zero)')

    problems = _recent_problems(report.rows, recent_problems)
    lines.append(f'recent problems (last {recent_problems}):')
    lines.extend(problems if problems else ['  (none)'])
    lines.append(divider)
    lines.append('orphaned (in the health store, not in any current config — may be deleted):')
    lines.extend([f'  {sid}' for sid in report.orphans] if report.orphans else ['  (none)'])
    return '\n'.join(lines)
