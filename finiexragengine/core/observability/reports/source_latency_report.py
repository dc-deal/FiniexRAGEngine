"""Source latency & gap report (ISSUE_76) — how the feeds actually behave, from the poll journal.

The read side of `source_poll_log`, and the answer to the two questions the 2026-08-15 `ecb_press`
incident could not answer:

**Was the feed slow or dead?** Success and failure latency are reported *separately*, which is what
makes the distinction readable: a failure whose duration sits at the configured timeout means the
feed accepted the connection and then went quiet (slow — a longer timeout might have worked); a
failure that returns in milliseconds means it was refused outright (dead — a longer timeout would
change nothing). Mixing the two into one percentile would have hidden exactly that.

**What did the outage cost?** A gap in a source's poll series is an outage, measured against that
source's *own* cadence — so a feed polled every 40s and one polled every 10min are judged by their
own normal, with no global threshold to tune. Because it reads gaps rather than quarantine records,
it also catches worker death, config changes and poll-floor changes.

Sibling of `perf_report.py` (same shape, same native `percentile_cont`, unpaid calls instead of
paid ones) and rendered next to `source_health_report.py` by `sources_cli`.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# A gap this many times a source's own median poll interval counts as an outage. Generous on
# purpose: a single skipped tick is scheduling jitter, not an incident.
_GAP_FACTOR = 5.0
# ...and never call anything under this an outage, however fast the source's cadence. Without the
# floor a feed polled every 40s would report an "outage" for every 3-minute hiccup.
_GAP_FLOOR_S = 300.0


@dataclass
class SourceLatencyRow:
    """One feed's fetch latency profile inside the window — successes and failures kept apart."""
    source_id: str
    polls: int                          # successful polls (the percentiles below describe these)
    p50_ms: Optional[float]
    p95_ms: Optional[float]
    p99_ms: Optional[float]
    max_ms: Optional[float]
    failures: int
    fail_p50_ms: Optional[float]        # median duration of the FAILED polls — the slow/dead tell
    timeout_seconds: Optional[int] = None   # the configured deadline this feed is judged against
    warn_ratio: float = 0.7             # travels with the row so the verdict needs no ambient config

    @property
    def nearing_timeout(self) -> bool:
        """True when successful polls run close enough to the deadline to fail on a slow day."""
        if self.timeout_seconds is None or self.p99_ms is None:
            return False
        return self.p99_ms >= self.timeout_seconds * 1000.0 * self.warn_ratio

    @property
    def failure_verdict(self) -> str:
        """Why the failures failed, as far as their duration can tell: timeout vs refusal.

        The whole point of measuring the failure path. A failure that burned the full deadline was
        a feed that went quiet mid-conversation; one that came back immediately was a feed that
        said no. Only the first is a candidate for a longer timeout.
        """
        if not self.failures or self.fail_p50_ms is None:
            return ''
        if self.timeout_seconds is None:
            return 'failed'
        # Within 10% of the deadline = it ran out of time rather than being refused.
        if self.fail_p50_ms >= self.timeout_seconds * 1000.0 * 0.9:
            return 'timeout'
        return 'refused'


@dataclass
class SourceGapRow:
    """One feed's poll-series gaps inside the window — outages measured against its own cadence."""
    source_id: str
    cadence_s: float                    # median interval between polls = this feed's normal
    gaps: int                           # gaps beyond the outage threshold
    longest_gap_s: float
    polls_missed: int                   # gap time / cadence — the cost, in polls not made
    # Set when the feed's LAST sample is already older than the outage threshold: it is not polling
    # right now. Separated from the historical gaps because it is the one that needs acting on.
    ongoing_s: Optional[float] = None


def _edge_gaps(first_ts: datetime, last_ts: datetime, window_start: datetime,
               now: datetime, threshold_s: float) -> Tuple[float, float]:
    """The two outages a gap-between-rows measure cannot see: `(leading, trailing)` seconds.

    Poll gaps are computed with `lag()` over the journal rows, which by construction only sees the
    distance *between* two samples. Two outages therefore stayed invisible, and both were found by
    the poll counter disagreeing with the gap section rather than by the section itself:

    - **Leading** — a feed already down when the window opened has no earlier row to be measured
      from. On 2026-08-17 `ecb_press` showed 5,129 polls against its peers' 8,727 (≈19h missing,
      the tail of its 24h quarantine) while the gap section reported nothing at all.
    - **Trailing** — a feed that stopped and never came back has no *later* row. This is the more
      urgent of the two: it is not history, it is a feed that is down **now**.

    `window_start` must be the later of the requested window and the journal's own first sample —
    otherwise every feed reports a leading outage for the time before the journal existed.

    Returns raw distances; the caller compares them against `threshold_s`. Zero-clamped, because a
    sample can sit marginally outside a window boundary computed a moment earlier.
    """
    leading = max(0.0, (first_ts - window_start).total_seconds())
    trailing = max(0.0, (now - last_ts).total_seconds())
    return leading, trailing


@dataclass
class SourceLatencyReport:
    since_label: str
    latency: List[SourceLatencyRow]
    gaps: List[SourceGapRow]
    warn_ratio: float = 0.7
    journal_missing: bool = False       # no poll journal yet (pre-migration or freshly enabled)
    # The oldest sample inside the window — the report's *real* reach, which is not the same as
    # its nominal window. The journal cannot be backfilled (durations were never recorded before
    # ISSUE_76), so for the first weeks after deploy "last 7d" is really "since we started
    # measuring". Saying so is the same honesty the coverage report's `from …` stamps carry.
    oldest_sample: Optional[datetime] = None

    @property
    def reach_seconds(self) -> Optional[float]:
        """How much history the numbers above actually rest on."""
        if self.oldest_sample is None:
            return None
        return (datetime.now(timezone.utc) - self.oldest_sample).total_seconds()


def build_source_latency_report(database_url: str, since: datetime, *, since_label: str = '7d',
                                timeouts: Optional[Dict[str, int]] = None,
                                warn_ratio: float = 0.7,
                                table: str = 'source_poll_log') -> SourceLatencyReport:
    """Aggregate per-source fetch latency and poll-series gaps for the window.

    `timeouts` maps `source_id` to the deadline it is configured with (source override, else the
    set default) — the report cannot know it, and without it "p99 2.3s" has nothing to be near.
    """
    timeouts = timeouts or {}
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # A database from before migration 004 is a valid, empty answer — not a crash. The
            # read side never creates or migrates; that is the migration runner's job alone.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (table,))
            if cur.fetchone()[0] == 0:
                return SourceLatencyReport(since_label, [], [], warn_ratio, journal_missing=True)

            # How far back the journal actually reaches inside the window — see `oldest_sample`.
            cur.execute(f'SELECT min(ts) FROM {table} WHERE ts >= %s', (since,))
            oldest = cur.fetchone()[0]

            # Successes and failures aggregated in one pass, kept in separate columns: FILTER
            # gives per-outcome percentiles without a self-join or a second round-trip.
            cur.execute(
                f'SELECT source_id, '
                "count(*) FILTER (WHERE outcome = 'ok'), "
                "percentile_cont(0.5)  WITHIN GROUP (ORDER BY duration_ms) "
                "  FILTER (WHERE outcome = 'ok'), "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) "
                "  FILTER (WHERE outcome = 'ok'), "
                "percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) "
                "  FILTER (WHERE outcome = 'ok'), "
                "max(duration_ms) FILTER (WHERE outcome = 'ok'), "
                "count(*) FILTER (WHERE outcome = 'failed'), "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) "
                "  FILTER (WHERE outcome = 'failed') "
                f'FROM {table} WHERE ts >= %s AND duration_ms IS NOT NULL '
                'GROUP BY source_id ORDER BY source_id', (since,))
            latency = [
                SourceLatencyRow(
                    source_id=source_id, polls=int(polls),
                    p50_ms=_maybe(p50), p95_ms=_maybe(p95), p99_ms=_maybe(p99), max_ms=_maybe(mx),
                    failures=int(failures), fail_p50_ms=_maybe(fail_p50),
                    timeout_seconds=timeouts.get(source_id), warn_ratio=warn_ratio)
                for source_id, polls, p50, p95, p99, mx, failures, fail_p50 in cur.fetchall()]

            # Gaps: lag() gives each poll's distance from the previous one; the source's own median
            # of those distances is its cadence, and anything far beyond it is an outage. The first
            # and last sample per source come along, because lag() cannot see an outage that spans
            # a window edge — see `_edge_gaps`.
            cur.execute(
                'WITH gaps AS ('
                '  SELECT source_id, ts, '
                '         EXTRACT(EPOCH FROM (ts - lag(ts) OVER '
                '           (PARTITION BY source_id ORDER BY ts))) AS gap_s '
                f'  FROM {table} WHERE ts >= %s), '
                'cadence AS ('
                '  SELECT source_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_s) AS median_s,'
                '         min(ts) AS first_ts, max(ts) AS last_ts '
                '  FROM gaps GROUP BY source_id) '
                'SELECT g.source_id, c.median_s, c.first_ts, c.last_ts, '
                '       count(*) FILTER (WHERE g.gap_s > greatest(c.median_s * %s, %s)), '
                '       coalesce(max(g.gap_s) FILTER '
                '         (WHERE g.gap_s > greatest(c.median_s * %s, %s)), 0), '
                '       coalesce(sum(g.gap_s) FILTER '
                '         (WHERE g.gap_s > greatest(c.median_s * %s, %s)), 0) '
                'FROM gaps g JOIN cadence c USING (source_id) '
                'WHERE c.median_s > 0 '
                'GROUP BY g.source_id, c.median_s, c.first_ts, c.last_ts '
                'ORDER BY 5 DESC, 6 DESC',
                (since, _GAP_FACTOR, _GAP_FLOOR_S, _GAP_FACTOR, _GAP_FLOOR_S,
                 _GAP_FACTOR, _GAP_FLOOR_S))
            # A leading edge is only an outage relative to when the JOURNAL starts, not to the
            # requested window: before `oldest` there is nothing to have missed.
            now = datetime.now(timezone.utc)
            window_start = max(since, oldest) if oldest is not None else since
            gaps = []
            for source_id, median_s, first_ts, last_ts, count, longest, lost_s in cur.fetchall():
                cadence = float(median_s)
                threshold = max(cadence * _GAP_FACTOR, _GAP_FLOOR_S)
                count, longest, lost_s = int(count), float(longest), float(lost_s)
                leading, trailing = _edge_gaps(first_ts, last_ts, window_start, now, threshold)
                ongoing = trailing if trailing > threshold else None
                for edge in (leading, trailing):
                    if edge > threshold:
                        count += 1
                        longest = max(longest, edge)
                        lost_s += edge
                if not count:
                    continue                    # a feed with no outage needs no row
                # Each gap still contains one legitimate interval, so subtract one poll per gap —
                # otherwise a perfectly normal cadence would read as a missed poll.
                missed = max(0, round(lost_s / cadence) - count)
                gaps.append(SourceGapRow(source_id, cadence, count, longest, missed,
                                         ongoing_s=ongoing))
    except psycopg.Error as exc:
        raise VectorStoreError(f'source latency report failed: {exc}') from exc
    return SourceLatencyReport(since_label, latency, gaps, warn_ratio, oldest_sample=oldest)


def _maybe(value: Optional[float]) -> Optional[float]:
    """Postgres returns NULL for a percentile over an empty filter — keep that as None."""
    return None if value is None else float(value)


def _secs(ms: Optional[float]) -> str:
    """A compact duration ('42ms', '0.4s', '10.0s', '—').

    Sub-second values stay in milliseconds on purpose: a refused connection comes back in ~40ms,
    and rendering that as `0.0s` reads like missing data — exactly where the slow-vs-dead verdict
    needs the number to be legible.
    """
    if ms is None:
        return '—'
    return f'{ms:.0f}ms' if ms < 1000 else f'{ms / 1000.0:.1f}s'


def _duration(seconds: float) -> str:
    """Compact span for a gap ('3m42s', '23h58m', '2d 4h')."""
    if seconds < 60:
        return f'{seconds:.0f}s'
    if seconds < 3600:
        return f'{int(seconds // 60)}m{int(seconds % 60):02d}s'
    if seconds < 172800:
        return f'{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m'
    return f'{int(seconds // 86400)}d {int((seconds % 86400) // 3600)}h'


def _reach_line(report: SourceLatencyReport) -> str:
    """State what the numbers rest on, so a young journal is never read as a full window.

    The journal starts empty at deploy and cannot be backfilled — unlike ISSUE_81, which corrected
    the whole archive because the envelopes were already stored, durations were simply never
    recorded before. So for the first weeks "last 7d" means "since we started measuring", and a
    p99 over two hours of samples deserves less trust than one over two weeks.
    """
    reach = report.reach_seconds
    if reach is None:
        return 'journal: empty'
    covered = _duration(reach)
    stamp = f'{report.oldest_sample:%m-%d %H:%M} UTC'
    # Below a day of history, say plainly that the percentiles are still settling.
    if reach < 86400:
        return f'journal covers {covered} (from {stamp}) — still filling; p99 needs a few hours'
    return f'journal covers {covered} (from {stamp})'


def format_source_latency_report(report: SourceLatencyReport) -> str:
    """Render both sections as the shared console pattern (title + window + dividers)."""
    divider = '-' * 88
    lines = [
        f'latency (last {report.since_label}) — successful polls; failures kept separate',
        _reach_line(report),
        divider,
        f'{"source":18} {"polls":>7} {"p50":>7} {"p95":>7} {"p99":>7} {"max":>7} '
        f'{"fails":>6} {"fail p50":>9}  why',
        divider,
    ]
    for row in report.latency:
        warn = ' ⚠' if row.nearing_timeout else ''
        lines.append(
            f'{row.source_id:18.18} {row.polls:>7} {_secs(row.p50_ms):>7} {_secs(row.p95_ms):>7} '
            f'{_secs(row.p99_ms):>7} {_secs(row.max_ms):>7} {row.failures:>6} '
            f'{_secs(row.fail_p50_ms):>9}  {row.failure_verdict}{warn}'.rstrip())
    if report.journal_missing:
        lines.append('(no poll journal yet — apply migration 004 and let the workers run)')
    elif not report.latency:
        lines.append('(no polls recorded in the window)')
    lines.append(divider)
    lines.append(f'why: `timeout` = the failure burned the deadline (the feed went quiet — a longer '
                 f'timeout might help)')
    lines.append(f'     `refused` = it failed immediately (the feed said no — a longer timeout '
                 f'would change nothing)')
    lines.append(f'  ⚠  p99 is within {(1 - report.warn_ratio) * 100:.0f}% of the configured '
                 f'timeout — review the value before it starts failing')

    lines.append('')
    lines.append(f'poll gaps (last {report.since_label}) — outages measured against each feed\'s '
                 f'own cadence')
    lines.append(divider)
    lines.append(f'{"source":18} {"cadence":>9} {"gaps":>6} {"longest":>10} {"polls missed":>13}'
                 f'  status')
    lines.append(divider)
    for gap in report.gaps:
        # A feed still not polling is the one row worth acting on — say so rather than leaving it
        # to be inferred from a large `longest`.
        status = f'STILL DOWN {_duration(gap.ongoing_s)}' if gap.ongoing_s else ''
        lines.append(f'{gap.source_id:18.18} {_duration(gap.cadence_s):>9} {gap.gaps:>6} '
                     f'{_duration(gap.longest_gap_s):>10} {gap.polls_missed:>13}  '
                     f'{status}'.rstrip())
    if not report.gaps:
        lines.append('(no feed stopped being polled for longer than 5x its own cadence)')
    lines.append(divider)
    return '\n'.join(lines)
