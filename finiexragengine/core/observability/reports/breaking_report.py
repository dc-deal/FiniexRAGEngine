"""Breaking-detection report — reaction time + the flagged→confirmed funnel (ISSUE_11).

Aggregated **from the store**, never from logs: the persisted envelopes are the source of truth
(CLAUDE.md — capture at the call, report from the store). Reaction time is a live measurement that
cannot be rebuilt after the fact, so it rides on fields captured at the event: the envelope's
`timestamp` (t3), each source's `published_at` (t0) and `fetched_at` (t1). The detector's
`flagged_at` lives in the corpus and feeds the funnel's numerator.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import psycopg

from finiexragengine.core.pipeline.breaking_episode import EPISODE_GAP
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# `EPISODE_GAP` (consecutive is_breaking within the gap = one episode; reaction anchors on the
# FIRST confirming envelope) is shared with the live path (breaking_episode) so the store report
# and the dashboard never diverge.


@dataclass
class PipelineBreaking:
    """One pipeline's breaking episodes + their reaction-time samples, inside the window."""
    pipeline_id: str
    confirmed: int = 0                                    # breaking episodes
    engine_reaction_s: List[float] = field(default_factory=list)   # t3 − earliest fetched_at
    end_to_end_s: List[float] = field(default_factory=list)        # t3 − earliest published_at


@dataclass
class BreakingEpisodeRow:
    """One confirmed episode in the window — for the per-episode listing (ISSUE_64): when it started,
    how long it lasted, and why (the LLM's `reasoning`, frozen at the episode start like the reaction
    time). A report-local shape, built and consumed here (CLAUDE.md — self-contained unit)."""
    pipeline_id: str
    symbol: str
    signal: str
    started: datetime
    duration_s: float                # last breaking pass − start (0 = single-pass episode)
    reason: str
    engine_s: Optional[float]
    end_to_end_s: Optional[float]


@dataclass
class BreakingReport:
    since_label: str
    rows: List[PipelineBreaking]
    flagged_candidates: int             # corpus breaking_candidate=TRUE in the window (all sets)
    confirmed_episodes: int
    episodes: List[BreakingEpisodeRow] = field(default_factory=list)   # per-episode listing (ISSUE_64)


def _parse_dt(value: str) -> datetime:
    # Pydantic serializes tz-aware ISO 8601; tolerate a trailing 'Z' too.
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _percentile(values: List[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (0..1) — None on an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def build_breaking_report(database_url: str, since: datetime, *, since_label: str = '7d',
                          outcomes_table: str = 'outcomes',
                          articles_table: str = 'articles') -> BreakingReport:
    """Aggregate confirmed breaking episodes + reaction times + the corpus flag count."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # No outcomes table yet = nothing produced; a clean empty report, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (outcomes_table,))
            if cur.fetchone()[0] == 0:
                return BreakingReport(since_label, [], 0, 0)
            cur.execute(
                f'SELECT pipeline_id, envelope FROM {outcomes_table} '
                "WHERE ts >= %s AND status <> 'error' ORDER BY pipeline_id, ts",
                (since,))
            rows = cur.fetchall()
            # Flagged candidates in the corpus within the window (shared across a set's pipelines).
            flagged = 0
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (articles_table,))
            if cur.fetchone()[0]:
                cur.execute(
                    f'SELECT count(*) FROM {articles_table} '
                    'WHERE breaking_candidate = TRUE AND flagged_at >= %s', (since,))
                flagged = int(cur.fetchone()[0])
    except psycopg.Error as exc:
        raise VectorStoreError(f'breaking report failed: {exc}') from exc

    return _aggregate(rows, flagged, since_label)


def _aggregate(rows: List[Tuple[str, object]], flagged: int,
               since_label: str) -> BreakingReport:
    """Group breaking occurrences into episodes + reaction samples — the DB-free core (tested)."""
    # Per (pipeline, symbol): the timeline of breaking occurrences, later grouped into episodes.
    # Each occurrence also carries its signal + reasoning, so the episode listing can show the why.
    Occurrence = Tuple[datetime, Optional[float], Optional[float], str, str]
    occ: Dict[Tuple[str, str], List[Occurrence]] = {}
    for pipeline_id, envelope in rows:
        env = envelope if isinstance(envelope, dict) else json.loads(envelope)
        t3 = _parse_dt(env['timestamp'])
        for result in env.get('result', []):
            if not result.get('is_breaking'):
                continue
            sources = result.get('sources', [])
            # Exclude estimated publish dates (a date-less feed falls back published := fetched) so
            # e2e does not collapse onto engine — the same rule as the live path (breaking_episode).
            published = [_parse_dt(s['published_at']) for s in sources
                         if s.get('published_at') and s['published_at'] != s.get('fetched_at')]
            fetched = [_parse_dt(s['fetched_at']) for s in sources if s.get('fetched_at')]
            end_to_end = (t3 - min(published)).total_seconds() if published else None
            engine = (t3 - min(fetched)).total_seconds() if fetched else None
            occ.setdefault((pipeline_id, result['symbol']), []).append(
                (t3, engine, end_to_end, result.get('signal', ''), result.get('reasoning', '')))

    per_pipeline: Dict[str, PipelineBreaking] = {}
    episodes: List[BreakingEpisodeRow] = []
    for (pipeline_id, symbol), events in occ.items():
        events.sort(key=lambda event: event[0])
        row = per_pipeline.setdefault(pipeline_id, PipelineBreaking(pipeline_id))
        last_ts: Optional[datetime] = None
        current: Optional[BreakingEpisodeRow] = None
        for t3, engine, end_to_end, signal, reason in events:
            # A new episode: the first breaking seen, or a re-break after a gap. Reaction time and
            # reason are sampled only here — later re-confirmations of the same story do not reset
            # them; each continuation only extends the episode's duration.
            if last_ts is None or (t3 - last_ts) > EPISODE_GAP:
                row.confirmed += 1
                if engine is not None:
                    row.engine_reaction_s.append(engine)
                if end_to_end is not None:
                    row.end_to_end_s.append(end_to_end)
                current = BreakingEpisodeRow(pipeline_id, symbol, signal, t3, 0.0, reason,
                                             engine, end_to_end)
                episodes.append(current)
            elif current is not None:
                current.duration_s = (t3 - current.started).total_seconds()   # ongoing → grow it
            last_ts = t3

    ordered = sorted(per_pipeline.values(), key=lambda row: row.pipeline_id)
    episodes.sort(key=lambda episode: (episode.pipeline_id, episode.started))
    return BreakingReport(since_label, ordered, flagged,
                          sum(row.confirmed for row in ordered), episodes)


def _fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return '—'
    return f'{seconds:.0f}s' if seconds < 90 else f'{seconds / 60:.1f}m'


def _fmt_pair(values: List[float]) -> str:
    median = _percentile(values, 0.5)
    if median is None:
        return '—'
    return f'{_fmt_seconds(median)} / {_fmt_seconds(_percentile(values, 0.9))}'


def format_breaking_report(report: BreakingReport) -> str:
    """Render the report as the shared console pattern (no per-run footer — this is an aggregate)."""
    divider = '-' * 72
    lines = [
        'Breaking Detection — reaction & funnel',
        f'window: last {report.since_label}',
        divider,
        f'{"pipeline":24} {"confirmed":>9}  {"engine react":>15}  {"end-to-end":>15}',
        f'{"":24} {"episodes":>9}  {"med / p90":>15}  {"med / p90":>15}',
        divider,
    ]
    for row in report.rows:
        lines.append(f'{row.pipeline_id:24} {row.confirmed:>9}  '
                     f'{_fmt_pair(row.engine_reaction_s):>15}  {_fmt_pair(row.end_to_end_s):>15}')
    if not report.rows:
        lines.append('(no confirmed breaking in the window)')
    lines.append(divider)
    # The funnel: flagged (corpus, LLM-free) → confirmed (LLM) → pushed (live channel, Stage C).
    lines.append(f'funnel: {report.flagged_candidates} flagged → '
                 f'{report.confirmed_episodes} confirmed → push (Stage C, pending)')
    lines.append('engine react = t3−earliest fetched_at (what we control) · '
                 'end-to-end = t3−earliest published_at (what the consumer feels)')

    # Per-episode listing (ISSUE_64): what broke this window, grouped by pipeline — when it started,
    # how long it lasted, and why (the LLM's reasoning). Edge-triggered, so one line per real episode.
    lines.append('')
    lines.append(f'Breaking episodes — last {report.since_label}')
    lines.append(divider)
    lines.append(f'  {"symbol":8} {"sig":4} {"started":>11}  {"dur":>6}  why')
    if not report.episodes:
        lines.append('  (none)')
    else:
        current_pipeline: Optional[str] = None
        for episode in report.episodes:
            if episode.pipeline_id != current_pipeline:
                lines.append(episode.pipeline_id)          # section header per pipeline
                current_pipeline = episode.pipeline_id
            started = episode.started.strftime('%m-%d %H:%M')
            duration = _fmt_seconds(episode.duration_s) if episode.duration_s else '—'
            reason = episode.reason if len(episode.reason) <= 60 else episode.reason[:59] + '…'
            lines.append(f'  {episode.symbol:8} {episode.signal:4} {started:>11}  '
                         f'{duration:>6}  {reason}')
    return '\n'.join(lines)
