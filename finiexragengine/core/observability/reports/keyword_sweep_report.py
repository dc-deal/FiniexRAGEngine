"""Keyword sweep (ISSUE_121) — what a vocabulary would flag, replayed over the stored corpus.

The keyword path is the one detection gate that needs no corroboration: a hit on a feed at or above
`keyword_source_weight` flags HIGH on its own. Yet terms are written into `detection.keywords` by
hand, and until now the only way to learn what they do was to deploy them and wait. The cluster
path has `detection_sweep`; this is the vocabulary half.

Two measurements on the live feeds (2026-09-12) are why it exists, and neither is visible without
the corpus:

- a broad central-bank vocabulary was **~17 % precise**, and the false positives were the banks
  themselves — statistics releases, rate-announcement *calendars*, data-portal updates;
- `monetary policy decision` matches **zero** rows while the ECB titles every decision
  *"Monetary policy decisions"*. A term that matches nothing looks exactly like a term whose event
  has not happened yet, so a zero is reported as a finding and the plural form is probed for.

**Two counts per term, never one.** `hits` is every match in the window; `gated` only those on
a feed at or above the gate. A term that fires only below it is dead vocabulary, and that is
invisible in a raw count.

**The pattern is the detector's own construction** (`utils/keyword_pattern`), never a second one —
otherwise the report would describe a matcher that is not the one running. SQL pre-filters the
candidate rows with the same vocabulary, Python attributes them per term. No LLM, no embedding call,
no write: there is no path from this report into spend.

Served-vs-stored differences are **not** this report's question. `corpus_text` owns that comparison
and measures it; this sweeps the stored text, which is what the detector sees.
"""
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Set, Tuple

import psycopg

from finiexragengine.core.observability.reports.corpus_text_report import KeywordSet
from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.utils.keyword_pattern import (
    build_keyword_pattern,
    sql_keyword_alternation,
)

# How many terms the concentration line sums — enough to show a vocabulary carried by two or three
# words, short enough that the line stays one sentence.
_TOP_TERMS = 3


@dataclass
class TermRow:
    """One term's replay: what it matched, what the gate kept, and where."""
    term: str
    hits: int = 0
    gated: int = 0
    feeds: Set[str] = field(default_factory=set)
    example: str = ''
    # A zero-hit term gets its plural probed (ISSUE_121). `monetary policy decision` fires nothing
    # while `Monetary policy decisions` is the ECB's own title for every rate decision — the miss is
    # only visible next to the form the feeds actually publish.
    plural_probe: str = ''
    plural_hits: int = 0

    @property
    def feed_count(self) -> int:
        return len(self.feeds)

    @property
    def dead(self) -> bool:
        """Matched nothing the gate would have acted on — vocabulary that cannot flag."""
        return self.gated == 0


@dataclass
class KeywordSweepReport:
    """One source set's vocabulary, replayed over its own feeds."""
    source_set_id: str
    since_label: str
    gate: float
    feeds_total: int = 0
    feeds_at_gate: int = 0
    articles: int = 0
    # False when the caller supplied terms — the same surface answers "what would this candidate
    # list do" and "what is our configured list actually doing", and the render says which.
    from_config: bool = True
    normalizer: Optional[str] = None
    terms: List[TermRow] = field(default_factory=list)

    @property
    def gated_hits(self) -> int:
        return sum(row.gated for row in self.terms)

    @property
    def dead_terms(self) -> List[str]:
        return [row.term for row in self.terms if row.dead]

    @property
    def plural_findings(self) -> List[TermRow]:
        """Zero-hit terms whose plural DOES match — the defect that motivated this report."""
        return [row for row in self.terms if not row.hits and row.plural_hits]

    @property
    def top_share(self) -> float:
        """Share of gated hits carried by the largest `_TOP_TERMS` terms.

        0.0 when nothing fired.
        """
        if not self.gated_hits:
            return 0.0
        top = sorted((row.gated for row in self.terms), reverse=True)[:_TOP_TERMS]
        return 100.0 * sum(top) / self.gated_hits


def _window_clause(since: datetime, source_ids: Sequence[str],
                   normalizer: Optional[str]) -> Tuple[str, List[object]]:
    """The window every query here shares: this set's feeds, the window, one text treatment.

    `normalizer` narrows to one treatment exactly as `detection_sweep` does — `''` selects the
    un-normalised rows, None takes whatever is there.
    """
    where = ['published_at >= %s', 'source_id = ANY(%s)']
    params: List[object] = [since, sorted(source_ids)]
    if normalizer is not None:
        where.append('coalesce(text_normalizer, %s) = %s')
        params += ['', normalizer]
    return ' AND '.join(where), params


def _ordered(report: KeywordSweepReport) -> KeywordSweepReport:
    """Rows by what fired, then by name — the same order whatever the corpus held.

    Applied on every return path, including the early ones: an empty corpus returning rows in the
    order they were typed would make two runs of one vocabulary look different for no reason.
    """
    report.terms.sort(key=lambda row: (-row.gated, -row.hits, row.term))
    return report


def build_keyword_sweep_report(database_url: str, since: datetime, *,
                               keyword_set: KeywordSet,
                               terms: Optional[Sequence[str]] = None,
                               since_label: str = '14d',
                               normalizer: Optional[str] = None,
                               articles_table: str = 'articles') -> KeywordSweepReport:
    """Replay `terms` (or the configured vocabulary) over this set's stored articles."""
    vocabulary = tuple(terms) if terms else tuple(keyword_set.keywords)
    gate = keyword_set.keyword_source_weight
    report = KeywordSweepReport(
        source_set_id=keyword_set.source_set_id, since_label=since_label, gate=gate,
        feeds_total=len(keyword_set.weights),
        feeds_at_gate=sum(1 for weight in keyword_set.weights.values() if weight >= gate),
        from_config=not terms, normalizer=normalizer,
        terms=[TermRow(term=term) for term in vocabulary])
    if not vocabulary or not keyword_set.weights:
        return _ordered(report)

    patterns = {term: build_keyword_pattern([term]) for term in vocabulary}
    rows_by_term = {row.term: row for row in report.terms}
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # No corpus yet is an empty sweep, not a crash — the guard `corpus_text` already keeps.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (articles_table,))
            if not cur.fetchone()[0]:
                return _ordered(report)
            where, params = _window_clause(since, list(keyword_set.weights), normalizer)
            cur.execute(f'SELECT count(*) FROM {articles_table} WHERE {where}', params)
            report.articles = int(cur.fetchone()[0])
            if not report.articles:
                return _ordered(report)

            # One pass: SQL narrows to rows the vocabulary touches at all, Python decides which
            # term each row belongs to. Per-term SQL would be one query per term over the same rows.
            cur.execute(
                f"SELECT source_id, title, coalesce(summary, '') FROM {articles_table} "
                f"WHERE {where} AND (title || ' ' || coalesce(summary, '')) ~* %s",
                [*params, sql_keyword_alternation(vocabulary)])
            # Which term's example currently comes from a gated feed — build state, not a
            # reported field: a headline that could not have flagged is the wrong thing to show
            # beside a term's counts, so a gated hit replaces an ungated example once.
            example_gated: Dict[str, bool] = {}
            for source_id, title, summary in cur.fetchall():
                text = f'{title} {summary}'
                gated = keyword_set.weights.get(source_id, 0.0) >= gate
                for term, pattern in patterns.items():
                    if pattern is None or not pattern.search(text):
                        continue
                    row = rows_by_term[term]
                    row.hits += 1
                    row.feeds.add(source_id)
                    if gated:
                        row.gated += 1
                    if term not in example_gated or (gated and not example_gated[term]):
                        row.example = title
                        example_gated[term] = gated
            _probe_plurals(cur, articles_table, report, where, params)
    except psycopg.Error as exc:
        raise VectorStoreError(f'keyword sweep failed: {exc}') from exc

    return _ordered(report)


def _probe_plurals(cur: psycopg.Cursor, table: str, report: KeywordSweepReport,
                   where: str, params: List[object]) -> None:
    """For every term that matched nothing, ask whether its plural does.

    The whole point of the report in one query: a zero is otherwise indistinguishable from "the
    event has not happened", and the ECB case proves the difference matters.
    """
    probes = {row.term: f'{row.term}s' for row in report.terms
              if not row.hits and not row.term.endswith('s')}
    if not probes:
        return
    patterns = {term: build_keyword_pattern([probe]) for term, probe in probes.items()}
    cur.execute(
        f"SELECT title, coalesce(summary, '') FROM {table} "
        f"WHERE {where} AND (title || ' ' || coalesce(summary, '')) ~* %s",
        [*params, sql_keyword_alternation(sorted(probes.values()))])
    rows = cur.fetchall()
    by_term = {row.term: row for row in report.terms}
    for term, probe in probes.items():
        pattern = patterns[term]
        if pattern is None:
            continue
        hits = sum(1 for title, summary in rows if pattern.search(f'{title} {summary}'))
        if hits:
            by_term[term].plural_probe = probe
            by_term[term].plural_hits = hits


def format_keyword_sweep_report(report: KeywordSweepReport, width: Optional[int] = None) -> str:
    """The shared console pattern: title + window line + `----` dividers + aligned columns."""
    term_width = width or shutil.get_terminal_size((100, 20)).columns
    divider = '-' * min(max(term_width - 1, 80), 110)
    source = 'configured' if report.from_config else 'supplied'
    lines = [
        f'keyword sweep · {report.source_set_id} · {report.since_label} · '
        f'{report.articles:,} articles · gate {report.gate:.2f} '
        f'({report.feeds_at_gate} of {report.feeds_total} feeds at or above)',
        f'vocabulary: {source}, {len(report.terms)} term(s)'
        + (f' · text treatment {report.normalizer or "(raw)"}' if report.normalizer is not None
           else ''),
        divider,
    ]
    if not report.terms:
        lines.append('(no vocabulary configured for this source set)')
        return '\n'.join(lines)

    example_width = max(len(divider) - 62, 20)
    lines.append(f'{"term":<34} {"hits":>6} {"gated":>6} {"feeds":>6}  example')
    for row in report.terms:
        example = row.example[:example_width] or '—'
        lines.append(f'{row.term[:34]:<34} {row.hits:>6} {row.gated:>6} {row.feed_count:>6}  '
                     f'{example}')
    lines.append(divider)
    lines.append(f'{len(report.terms)} terms · {report.gated_hits:,} gated hits · '
                 f'{report.top_share:.1f} % of them from the top '
                 f'{min(_TOP_TERMS, len(report.terms))}')
    if report.dead_terms:
        lines.append('dead vocabulary — matched nothing the gate would act on: '
                     + ', '.join(f'`{term}`' for term in report.dead_terms))
    for row in report.plural_findings:
        lines.append(f'⚠️  `{row.term}` matches 0 rows while `{row.plural_probe}` matches '
                     f'{row.plural_hits} — word boundaries are exact and config values are '
                     f'escaped, so list the form the feed publishes')
    lines.append('served-vs-stored differences are `corpus_text`\'s question, not this one')
    return '\n'.join(lines)
