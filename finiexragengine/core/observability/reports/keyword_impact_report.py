"""Keyword impact (ISSUE_124) — what each vocabulary term actually did, from flag to envelope.

`keyword_sweep` (ISSUE_121) is prospective: it replays a vocabulary over the stored corpus and says
what it *would* flag. That was enough to ship a verified forex vocabulary on 2026-09-14. It cannot
say whether a shipped term changed anything, and a term is worth having only if its flag reached an
envelope earlier than the scheduled pass would have.

Three failures are invisible without this report, and every one of them is **per term** — the unit a
vocabulary decision is taken in:

- **a flag nobody cited.** The article raised a tier, the pass woke, and retrieval never surfaced it:
  the wake bought an LLM call and no evidence;
- **a flag that beat the tick by seconds.** `flagged_at` → envelope is the whole saving;
- **a flag whose envelope sits on the baseline.** Same urgency as the scheduled passes around it, so
  the wake produced an earlier identical answer rather than a different one.

**Nothing here is new data.** Migration 014 records which terms fired (`detection_keywords`),
`trigger_reason` records the wake, `ArticleRef.article_id` records what reached the answer, and
`breaking_report` already does this arithmetic — ungrouped. This report joins them by term.

**Citation is the proof, not the timestamp.** A reaction time is computed only where the woken
envelope cites the flagged article: without that, the number is arithmetic about an article nobody
read. Citation anywhere else is *reach*, and it is a footer line rather than a column, so the two can
never be read as the same measure.

Read-only over `articles` + `outcomes`: no LLM, no embedding call, no write.
"""
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import fmean, median
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import psycopg

from finiexragengine.core.observability.reports.corpus_text_report import KeywordSet
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# How long after a flag a breaking-triggered envelope may still be that flag's wake. The bus
# publishes on the pass that flagged, so the eval starts within seconds; a breaking envelope a
# quarter of an hour later belongs to a different story, and claiming it would invent a saving.
_WAKE_WINDOW = timedelta(minutes=15)

# What a flag with no recorded vocabulary is called. NULL means the terms were never written (a
# cluster flag, or anything flagged before migration 014) — an absence, never a term that fired.
_UNRECORDED = 'unrecorded'


@dataclass
class TermImpact:
    """One term's chain: how often it fired, and what came of it."""
    term: str
    flags: int = 0
    articles: Set[str] = field(default_factory=set)
    woke: int = 0                                   # flags attributed to a breaking envelope
    cited: int = 0                                  # ...whose envelope also cited the article
    confirmed: int = 0                              # ...and carried `is_breaking`
    # Seconds from `flagged_at` to the envelope, for cited flags only (see the module docstring).
    reactions: List[float] = field(default_factory=list)
    # One mean urgency per woken envelope — compared against the report's baseline, never alone.
    urgencies: List[float] = field(default_factory=list)
    example: str = ''

    @property
    def article_count(self) -> int:
        return len(self.articles)

    @property
    def reaction_s(self) -> Optional[float]:
        """Median seconds from flag to envelope. None when nothing this term flagged was cited."""
        return median(self.reactions) if self.reactions else None

    @property
    def urgency(self) -> Optional[float]:
        return fmean(self.urgencies) if self.urgencies else None

    @property
    def unread(self) -> int:
        """Woke a pass and was not cited by it — the cheapest waste this report can name."""
        return self.woke - self.cited


@dataclass
class KeywordImpactReport:
    """One source set's shipped vocabulary, measured through to the envelopes it woke."""
    source_set_id: str
    since_label: str
    pipelines: List[str] = field(default_factory=list)
    vocabulary: List[str] = field(default_factory=list)
    flags: int = 0
    flagged_articles: int = 0
    unrecorded: int = 0                             # keyword flags carrying no terms (see above)
    reached: int = 0                                # flagged articles cited in ANY envelope (reach)
    baseline_urgency: Optional[float] = None        # scheduled passes, same pipelines, same window
    woken_urgency: Optional[float] = None           # breaking passes, same window
    terms: List[TermImpact] = field(default_factory=list)

    @property
    def silent_terms(self) -> List[str]:
        """Configured terms that never fired in the window — named, never rendered as zero rows."""
        fired = {row.term for row in self.terms}
        return [term for term in self.vocabulary if term not in fired]

    def delta(self, row: TermImpact) -> Optional[float]:
        """This term's urgency against the baseline. None when either side has no sample."""
        if row.urgency is None or self.baseline_urgency is None:
            return None
        return row.urgency - self.baseline_urgency


@dataclass
class _Pass:
    """One persisted envelope, reduced to what the join needs."""
    pipeline_id: str
    at: datetime
    breaking: bool                                  # woken out of band (`trigger_reason`)
    cited: Set[str] = field(default_factory=set)    # `ArticleRef.article_id`s across all symbols
    urgency: Optional[float] = None                 # mean over the symbols that carried one
    confirmed: bool = False                         # any symbol row with `is_breaking`


@dataclass
class _Flag:
    """One keyword flag in the window, with the terms recorded for it."""
    article_id: str
    title: str
    at: datetime
    terms: Tuple[str, ...]


def _ordered(report: KeywordImpactReport) -> KeywordImpactReport:
    """Rows by what fired most, then by name — the same order whatever the window held.

    On every return path, including the early ones: two runs over one vocabulary must not differ in
    order for no reason.
    """
    report.terms.sort(key=lambda row: (-row.flags, -row.cited, row.term))
    return report


def _envelope_time(envelope: Dict[str, Any], persisted: datetime) -> datetime:
    """The envelope's own analysis timestamp, falling back to when the store wrote it.

    The envelope's value is the one `breaking_report` measures reaction against, so both surfaces
    answer with the same number; the fallback keeps a row whose timestamp is missing or unparseable
    inside the report instead of dropping it.
    """
    stamp = envelope.get('timestamp')
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        except ValueError:
            return persisted
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return persisted


def _read_pass(pipeline_id: str, envelope: Dict[str, Any], persisted: datetime) -> _Pass:
    """Reduce one envelope to the fields the join needs — cited articles, urgency, breaking."""
    urgencies: List[float] = []
    cited: Set[str] = set()
    confirmed = False
    for result in envelope.get('result') or []:
        if result.get('urgency') is not None:
            urgencies.append(float(result['urgency']))
        confirmed = confirmed or bool(result.get('is_breaking'))
        for source in result.get('sources') or []:
            article_id = source.get('article_id')
            if article_id:
                cited.add(article_id)
    return _Pass(pipeline_id=pipeline_id, at=_envelope_time(envelope, persisted),
                 breaking=envelope.get('trigger_reason') == 'breaking', cited=cited,
                 urgency=fmean(urgencies) if urgencies else None, confirmed=confirmed)


def _wake_for(flag: _Flag, passes: Sequence[_Pass]) -> Optional[_Pass]:
    """The breaking envelope this flag woke, or None.

    The first breaking-triggered pass at or after `flagged_at`, inside `_WAKE_WINDOW`. Two rules
    live in that sentence and both matter: a pass that was **already running** when the flag landed
    cannot have been woken by it, and a breaking pass a quarter of an hour later belongs to another
    story. Several flags from one ingest pass legitimately share one envelope — that is one wake for
    two articles, not two wakes.
    """
    for entry in passes:                             # passes arrive sorted by time
        if not entry.breaking or entry.at < flag.at:
            continue
        if entry.at - flag.at > _WAKE_WINDOW:
            return None
        return entry
    return None


def build_keyword_impact_report(database_url: str, since: datetime, *,
                                keyword_set: KeywordSet,
                                pipeline_ids: Sequence[str],
                                since_label: str = '30d',
                                articles_table: str = 'articles',
                                outcomes_table: str = 'outcomes') -> KeywordImpactReport:
    """Join this set's keyword flags to the envelopes they woke, grouped by term."""
    report = KeywordImpactReport(source_set_id=keyword_set.source_set_id, since_label=since_label,
                                 pipelines=sorted(pipeline_ids),
                                 vocabulary=list(keyword_set.keywords))
    if not keyword_set.weights:
        return _ordered(report)
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            flags = _flags(cur, articles_table, since, sorted(keyword_set.weights))
            passes = _passes(cur, outcomes_table, since, report.pipelines)
    except psycopg.Error as exc:
        raise VectorStoreError(f'keyword impact failed: {exc}') from exc

    return _ordered(_aggregate(report, flags, passes))


def _flags(cur: psycopg.Cursor, table: str, since: datetime,
           source_ids: Sequence[str]) -> List[_Flag]:
    """Every keyword flag on this set's feeds inside the window.

    Guarded twice, the shape `breaking_report` already uses: a database without the table is an
    empty report rather than a crash, and `detection_keywords` is checked in the catalog before it
    is selected — where migration 014 has not run, the flags are counted as unrecorded instead of
    every term reading as "never fired".
    """
    cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s', (table,))
    if not cur.fetchone()[0]:
        return []
    cur.execute('SELECT count(*) FROM information_schema.columns '
                'WHERE table_name = %s AND column_name = %s', (table, 'detection_keywords'))
    terms_column = 'detection_keywords' if cur.fetchone()[0] else 'NULL'
    cur.execute(
        f'SELECT article_id, title, flagged_at, {terms_column} FROM {table} '
        "WHERE detection_trigger = 'keyword' AND flagged_at >= %s AND source_id = ANY(%s) "
        'ORDER BY flagged_at',
        (since, list(source_ids)))
    return [_Flag(article_id=article_id, title=title, at=flagged_at, terms=tuple(terms or ()))
            for article_id, title, flagged_at, terms in cur.fetchall()]


def _passes(cur: psycopg.Cursor, table: str, since: datetime,
            pipeline_ids: Sequence[str]) -> List[_Pass]:
    """Every envelope of the pipelines that read this source set, oldest first."""
    cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s', (table,))
    if not cur.fetchone()[0] or not pipeline_ids:
        return []
    cur.execute(
        f'SELECT pipeline_id, ts, envelope FROM {table} '
        "WHERE ts >= %s AND status <> 'error' AND pipeline_id = ANY(%s) ORDER BY ts",
        (since, list(pipeline_ids)))
    return [_read_pass(pipeline_id, envelope, persisted)
            for pipeline_id, persisted, envelope in cur.fetchall()]


def _aggregate(report: KeywordImpactReport, flags: Sequence[_Flag],
               passes: Sequence[_Pass]) -> KeywordImpactReport:
    """The DB-free core: attribute every flag, then group by term (tested directly)."""
    report.flags = len(flags)
    report.flagged_articles = len({flag.article_id for flag in flags})
    report.unrecorded = sum(1 for flag in flags if not flag.terms)
    # The two population means the per-term delta is read against. Both are printed: a delta whose
    # baseline is invisible is a number nobody can check.
    measured = [entry for entry in passes if entry.urgency is not None]
    baseline = [entry.urgency for entry in measured if not entry.breaking]
    woken = [entry.urgency for entry in measured if entry.breaking]
    report.baseline_urgency = fmean(baseline) if baseline else None
    report.woken_urgency = fmean(woken) if woken else None
    # Reach: cited by ANY envelope, woken or scheduled. Deliberately not the `cited` column — an
    # article the next scheduled pass picked up was read, but not *because* of the flag.
    cited_anywhere = {article_id for entry in passes for article_id in entry.cited}
    report.reached = len({flag.article_id for flag in flags if flag.article_id in cited_anywhere})

    rows: Dict[str, TermImpact] = {}
    for flag in flags:
        wake = _wake_for(flag, passes)
        for term in flag.terms:
            row = rows.setdefault(term, TermImpact(term=term))
            row.flags += 1
            row.articles.add(flag.article_id)
            if not row.example:
                row.example = flag.title
            if wake is None:
                continue
            row.woke += 1
            if wake.urgency is not None:
                row.urgencies.append(wake.urgency)
            if flag.article_id in wake.cited:
                row.cited += 1
                row.reactions.append((wake.at - flag.at).total_seconds())
                if wake.confirmed:
                    row.confirmed += 1
    report.terms = list(rows.values())
    return report


def _minutes(seconds: Optional[float]) -> str:
    return '—' if seconds is None else f'{seconds / 60:.1f} min'


def _delta(value: Optional[float]) -> str:
    return '—' if value is None else f'{value:+.2f}'


def format_keyword_impact_report(report: KeywordImpactReport,
                                 width: Optional[int] = None) -> str:
    """The shared console pattern: title + window line + `----` dividers + aligned columns."""
    term_width = width or shutil.get_terminal_size((100, 20)).columns
    divider = '-' * min(max(term_width - 1, 80), 110)
    fired = len(report.terms)
    lines = [
        f'keyword impact · {report.source_set_id} · {report.since_label} · '
        f'{report.flags:,} flags on {report.flagged_articles:,} articles · '
        f'{fired} of {len(report.vocabulary)} configured terms fired',
        f'pipelines: {", ".join(report.pipelines) or "(none reads this set)"}',
        divider,
    ]
    if not report.terms:
        # Two different empty reports, and the header's flag count makes the difference visible:
        # nothing fired at all, or flags exist whose terms were never recorded. Saying "no keyword
        # flag" above a header reading "10 flags" would be the report contradicting itself.
        if report.unrecorded:
            lines.append(f'{report.unrecorded:,} keyword flag(s) carry no vocabulary — flagged '
                         f'before the terms were recorded (migration 014), so which term fired '
                         f'cannot be answered for them')
        else:
            lines.append('no keyword flag in this window — `keyword_sweep` says whether the corpus '
                         'offered the vocabulary a chance')
        return '\n'.join(lines)

    # 73 = the fixed columns before it, so the table never runs past its own divider.
    example_width = max(len(divider) - 73, 20)
    lines.append(f'{"term":<28} {"flags":>5} {"cited":>5} {"woke":>5} {"reaction":>9} '
                 f'{"urgency Δ":>9} {"conf":>4}  example')
    for row in report.terms:
        lines.append(f'{row.term[:28]:<28} {row.flags:>5} {row.cited:>5} {row.woke:>5} '
                     f'{_minutes(row.reaction_s):>9} {_delta(report.delta(row)):>9} '
                     f'{row.confirmed:>4}  {row.example[:example_width] or "—"}')
    lines.append(divider)
    baseline = ('unavailable — no scheduled pass in this window'
                if report.baseline_urgency is None else f'{report.baseline_urgency:.2f}')
    woken = 'none' if report.woken_urgency is None else f'{report.woken_urgency:.2f}'
    lines.append(f'baseline urgency (scheduled passes, same pipelines, same window): {baseline} · '
                 f'woken passes: {woken}')
    lines.append(f'reach: {report.flagged_articles:,} flagged articles, {report.reached:,} cited '
                 f'anywhere — {report.flagged_articles - report.reached:,} raised a tier and were '
                 f'never read')
    unread = [row for row in report.terms if row.unread]
    if unread:
        lines.append('woke a pass and was not cited by it: '
                     + ' · '.join(f'`{row.term}` {row.unread}' for row in unread))
    if report.unrecorded:
        lines.append(f'{report.unrecorded:,} keyword flag(s) carry no vocabulary — flagged before '
                     f'the terms were recorded (migration 014), counted nowhere above')
    if report.silent_terms:
        lines.append(f'{len(report.silent_terms)} configured term(s) never fired: '
                     + ', '.join(f'`{term}`' for term in report.silent_terms))
    lines.append('what a term WOULD flag is `keyword_sweep`\'s question, not this one')
    return '\n'.join(lines)
