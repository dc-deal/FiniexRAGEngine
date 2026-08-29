"""Corpus text — what treatment produced the stored text, and what it removed (ISSUE_112).

The normaliser's effect was measurable in exactly one place: a SQL prompt on the production box.
Everything the engine surfaced was the *at-the-call* echo — `normalised N (M chars)` on a pass line,
gone as soon as the next pass overwrote it. So "is the treatment still working, and how far has the
corpus turned over" was a question only an operator with a shell could answer, which is the shape
CLAUDE.md names as a threshold nobody can tune.

Four questions, one surface:

1. **The stock** — how much of the corpus carries which profile. The transition is forward-only
   (`ON CONFLICT DO NOTHING`), so this climbs as feeds publish and never by rewriting history.
2. **The proof** — carriers per treatment. A stamped row with markup in it is the normaliser
   failing, and it is the one number that cannot be argued with.
3. **The removal** — measured WITHIN a row, from the kept raw copy, so no drift in article length
   between two periods can flatter or spoil it.
4. **The keyword fast path** — matches that exist only inside markup. Six of 99 dev keyword hits
   were a CDN's stock-image filenames on a weight-1.0 source, and that source alone flags an article
   HIGH. This is the half that changes signals rather than costs.

Question 4 is evaluated with the **real** `ArticleNormalizer`, not a SQL imitation of it: a report
that approximates the treatment it audits can only ever measure its own approximation.
"""
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg

from finiexragengine.core.sources.article_normalizer import ArticleNormalizer
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# What a NULL `text_normalizer` renders as: stored before the treatment existed. Deliberately not
# folded into a profile — it is an absence, exactly as `unrecorded` is for `detection_trigger`.
UNSTAMPED = 'unstamped'

# Carrier patterns, evaluated in SQL so the corpus-wide census costs one scan rather than a transfer
# of every article's text. They mirror `ArticleNormalizer`'s targets without being its
# implementation — their job is to *detect* a carrier, not to remove one.
#
# The markup bound is 255, not the normaliser's 400: PostgreSQL caps a regex repetition count at
# 255. Over 11,994 measured tags the p99 is 238, so the two agree on all but a fraction of a
# percent — and the difference is named here rather than discovered by whoever compares the report
# against the code.
_SQL_MARKUP = r'<[a-zA-Z/!][^>]{0,255}>'
_SQL_ENTITY = r'&[a-zA-Z]{2,8};|&#[0-9]{2,6};'
# A plain (non-raw) string so the SOURCE stays reviewable ASCII while the VALUE is the actual
# characters — an invisible character pasted into a file cannot be reviewed, diffed or safely
# copied. The pattern travels as a query parameter, so it must already hold the characters:
# PostgreSQL's own E'\\uXXXX' expansion applies to inline literals, never to a bound value.
_SQL_ZERO_WIDTH = '[\u200B-\u200F\u2060-\u2064\uFEFF]'


@dataclass
class TreatmentCensus:
    """One text treatment's slice of the corpus, and what its rows still carry."""
    profile: str                 # 'v1', … or UNSTAMPED
    articles: int = 0
    with_markup: int = 0
    with_entities: int = 0
    with_zero_width: int = 0

    @property
    def clean(self) -> bool:
        """No carrier survived in this slice — what a working treatment must produce."""
        return not (self.with_markup or self.with_entities or self.with_zero_width)

    def share_of(self, total: int) -> float:
        return self.articles / total if total else 0.0


@dataclass
class RemovalStats:
    """What the treatment removed, measured within each row against its own kept original.

    Within-row on purpose: comparing a normalised period against an un-normalised one would let a
    drift in article length pass for a change in markup. Rows that arrived clean keep NULL raws and
    contribute to the denominator only — that is what makes the percentage a corpus share rather
    than a per-dirty-article one.
    """
    rows: int = 0                # rows carrying a profile
    rows_changed: int = 0        # of those, rows where the text actually changed
    chars_served: int = 0        # as the feed served it
    chars_stored: int = 0        # as the model reads it

    @property
    def chars_removed(self) -> int:
        return self.chars_served - self.chars_stored

    @property
    def removed_share(self) -> float:
        return self.chars_removed / self.chars_served if self.chars_served else 0.0

    @property
    def changed_share(self) -> float:
        return self.rows_changed / self.rows if self.rows else 0.0


@dataclass
class PhantomSource:
    """One feed's keyword hits that exist ONLY inside markup — the false-positive class.

    `self_flags` is the whole point: the keyword fast path raises an article to HIGH with no cluster
    and no corroboration when the source's weight clears the gate, so a phantom hit on such a feed
    is a breaking candidate invented by a CDN's filename.
    """
    source_id: str
    source_set_id: str
    weight: float
    gate: float
    phantom_hits: int = 0        # matched as served, not after normalising
    prose_hits: int = 0          # matched in both — the genuine ones
    examples: List[str] = field(default_factory=list)

    @property
    def self_flags(self) -> bool:
        return self.weight >= self.gate


@dataclass
class CorpusTextReport:
    since_label: str
    articles: int = 0                                  # corpus-wide
    treatments: List[TreatmentCensus] = field(default_factory=list)
    removal: RemovalStats = field(default_factory=RemovalStats)
    phantoms: List[PhantomSource] = field(default_factory=list)
    # The flow, not the stock: what arrived inside the window. Answers "is it still working right
    # now", which the corpus-wide numbers cannot — they are dominated by history for months.
    window_articles: int = 0
    window_stamped: int = 0
    # Source-sets whose keyword list was actually checked, so an empty phantom table reads as
    # "checked and none found" instead of "nothing ran".
    keyword_sets: List[str] = field(default_factory=list)
    # Feeds that appear in the corpus under no configured set — their phantom hits cannot be
    # judged, because the gate that would flag them is unknown.
    orphan_sources: List[str] = field(default_factory=list)

    @property
    def stamped(self) -> int:
        return sum(t.articles for t in self.treatments if t.profile != UNSTAMPED)

    @property
    def dirty_stamped(self) -> List[TreatmentCensus]:
        """Stamped slices that still carry a carrier — empty is the passing state."""
        return [t for t in self.treatments if t.profile != UNSTAMPED and not t.clean]

    @property
    def phantom_total(self) -> int:
        return sum(p.phantom_hits for p in self.phantoms)

    @property
    def phantom_self_flagging(self) -> int:
        return sum(p.phantom_hits for p in self.phantoms if p.self_flags)


# --- the keyword half: resolved from config, evaluated with the real normaliser ----------------

@dataclass
class KeywordSet:
    """One source-set's detection vocabulary and the gate its feeds are judged by.

    Carried in rather than read here for the same reason every other report takes its config:
    the registry factories are the only load path that honours the `user_configs/` overlay, and a
    report resolving its own config would silently describe a configuration that did not run.
    """
    source_set_id: str
    keywords: Sequence[str]
    keyword_source_weight: float
    weights: Dict[str, float]           # source_id -> configured weight


def build_corpus_text_report(database_url: str, since: datetime, *,
                             since_label: str = '7d',
                             articles_table: str = 'articles',
                             keyword_sets: Optional[Sequence[KeywordSet]] = None,
                             example_limit: int = 3,
                             ) -> CorpusTextReport:
    """Census the corpus by text treatment, and re-test the keyword path against the normaliser."""
    report = CorpusTextReport(since_label)
    normalizer = ArticleNormalizer()
    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
            # A database without the corpus is not an error: a fresh install has produced nothing,
            # and the honest answer is an empty report. (The branch that promised this in
            # `breaking_report` and did not deliver it is what taught this file to test it.)
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (articles_table,))
            if cur.fetchone()[0] == 0:
                return report
            # Equally: a corpus that predates migration 012 has no stamp to census. Guarded rather
            # than assumed, the same way `breaking_report` guards `detection_trigger`.
            cur.execute('SELECT count(*) FROM information_schema.columns '
                        'WHERE table_name = %s AND column_name = %s',
                        (articles_table, 'text_normalizer'))
            stamped_column = bool(cur.fetchone()[0])

            cur.execute(f'SELECT count(*) FROM {articles_table}')
            report.articles = int(cur.fetchone()[0])
            if not report.articles:
                return report

            _census(cur, articles_table, report, stamped_column)
            _window(cur, articles_table, report, since, stamped_column)
            if stamped_column:
                _removal(cur, articles_table, report)
            _phantoms(cur, articles_table, report, normalizer, keyword_sets or (), example_limit)
    except psycopg.Error as exc:
        raise VectorStoreError(f'corpus text report failed: {exc}') from exc
    return report


def _census(cur: psycopg.Cursor, table: str, report: CorpusTextReport,
            stamped_column: bool) -> None:
    """Articles and surviving carriers, grouped by the treatment that produced the row."""
    profile = 'coalesce(text_normalizer, %s)' if stamped_column else '%s'
    cur.execute(
        f'SELECT {profile} AS profile, count(*), '
        f"count(*) FILTER (WHERE (title || ' ' || summary) ~ %s), "
        f"count(*) FILTER (WHERE (title || ' ' || summary) ~ %s), "
        f"count(*) FILTER (WHERE (title || ' ' || summary) ~ %s) "
        f'FROM {table} GROUP BY 1 ORDER BY 1',
        (UNSTAMPED, _SQL_MARKUP, _SQL_ENTITY, _SQL_ZERO_WIDTH))
    report.treatments = [
        TreatmentCensus(profile=str(row[0]), articles=int(row[1]), with_markup=int(row[2]),
                        with_entities=int(row[3]), with_zero_width=int(row[4]))
        for row in cur.fetchall()
    ]
    # Unstamped last: the report reads as a transition, and the slice being replaced belongs at the
    # bottom of it rather than sorted alphabetically into the middle.
    report.treatments.sort(key=lambda t: (t.profile == UNSTAMPED, t.profile))


def _window(cur: psycopg.Cursor, table: str, report: CorpusTextReport, since: datetime,
            stamped_column: bool) -> None:
    """The flow: what arrived in the window, and how much of it carried a stamp."""
    if stamped_column:
        cur.execute(f'SELECT count(*), count(*) FILTER (WHERE text_normalizer IS NOT NULL) '
                    f'FROM {table} WHERE fetched_at >= %s', (since,))
        total, stamped = cur.fetchone()
        report.window_articles, report.window_stamped = int(total), int(stamped)
        return
    cur.execute(f'SELECT count(*) FROM {table} WHERE fetched_at >= %s', (since,))
    report.window_articles = int(cur.fetchone()[0])


def _removal(cur: psycopg.Cursor, table: str, report: CorpusTextReport) -> None:
    """Characters served vs stored, per row, over the rows a treatment actually produced."""
    cur.execute(
        'SELECT count(*), '
        'count(*) FILTER (WHERE title_raw IS NOT NULL OR summary_raw IS NOT NULL), '
        'coalesce(sum(length(coalesce(title_raw, title)) '
        '           + length(coalesce(summary_raw, summary))), 0), '
        'coalesce(sum(length(title) + length(summary)), 0) '
        f'FROM {table} WHERE text_normalizer IS NOT NULL')
    rows, changed, served, stored = cur.fetchone()
    report.removal = RemovalStats(rows=int(rows), rows_changed=int(changed),
                                  chars_served=int(served), chars_stored=int(stored))


def _phantoms(cur: psycopg.Cursor, table: str, report: CorpusTextReport,
              normalizer: ArticleNormalizer, keyword_sets: Sequence[KeywordSet],
              example_limit: int) -> None:
    """Keyword hits that survive as served but not after normalising — per feed.

    Only rows that match as served are transferred, which is the small minority (99 of 1,966 in
    dev), so the real normaliser can be applied to each without pulling the corpus into memory.
    """
    known: Dict[str, Tuple[KeywordSet, float]] = {}
    for keyword_set in keyword_sets:
        if not keyword_set.keywords:
            continue
        report.keyword_sets.append(keyword_set.source_set_id)
        pattern = re.compile(r'\b(?:' + '|'.join(re.escape(k) for k in keyword_set.keywords)
                             + r')\b', re.IGNORECASE)
        sql_pattern = r'\y(' + '|'.join(_sql_quote(k) for k in keyword_set.keywords) + r')\y'
        for source_id, weight in keyword_set.weights.items():
            known[source_id] = (keyword_set, weight)
        cur.execute(
            f"SELECT source_id, title, summary FROM {table} "
            f"WHERE source_id = ANY(%s) AND (title || ' ' || summary) ~* %s",
            (list(keyword_set.weights), sql_pattern))
        found: Dict[str, PhantomSource] = {}
        for source_id, title, summary in cur.fetchall():
            row = found.get(source_id)
            if row is None:
                row = PhantomSource(source_id=source_id,
                                    source_set_id=keyword_set.source_set_id,
                                    weight=keyword_set.weights.get(source_id, 0.0),
                                    gate=keyword_set.keyword_source_weight)
                found[source_id] = row
            served = f'{title} {summary}'
            stored = f'{normalizer.normalize_text(title)} {normalizer.normalize_text(summary)}'
            if pattern.search(stored):
                row.prose_hits += 1
                continue
            row.phantom_hits += 1
            if len(row.examples) < example_limit:
                match = pattern.search(served)
                if match is not None:
                    row.examples.append(served[max(0, match.start() - 50):match.end() + 25])
        report.phantoms.extend(r for r in found.values() if r.phantom_hits)
    report.phantoms.sort(key=lambda p: (-p.phantom_hits, p.source_id))

    # Feeds present in the corpus that no configured set claims: their hits cannot be judged,
    # because the gate that would flag them is unknown. Named rather than silently excluded.
    cur.execute(f'SELECT DISTINCT source_id FROM {table}')
    report.orphan_sources = sorted(str(row[0]) for row in cur.fetchall()
                                   if str(row[0]) not in known)


def _sql_quote(keyword: str) -> str:
    """Escape a keyword for a POSIX alternation — the vocabulary is operator-written config."""
    return re.sub(r'([\\.^$|()\[\]{}*+?])', r'\\\1', keyword)


def format_corpus_text_report(report: CorpusTextReport, width: Optional[int] = None) -> str:
    """The shared console pattern: title + window line + `----` dividers + aligned columns."""
    term_width = width or shutil.get_terminal_size((100, 20)).columns
    divider = '-' * min(term_width, 92)
    lines = [
        'Corpus text — treatment, carriers and what the normaliser removed',
        f'corpus: {report.articles:,} articles (all time) · window: last {report.since_label} '
        f'(the flow only)',
        divider,
    ]
    if not report.articles:
        lines.append('(the corpus is empty)')
        return '\n'.join(lines)

    # 1. the stock, by treatment
    lines.append(f'{"treatment":<12} {"articles":>9} {"share":>7} {"markup":>8} {"entities":>9} '
                 f'{"zero-w":>7}')
    lines.append(divider)
    for treatment in report.treatments:
        lines.append(
            f'{treatment.profile:<12} {treatment.articles:>9,} '
            f'{treatment.share_of(report.articles) * 100:>6.1f}% '
            f'{treatment.with_markup:>8,} {treatment.with_entities:>9,} '
            f'{treatment.with_zero_width:>7,}')
    lines.append(divider)

    # 2. the verdict, stated rather than left to the reader
    dirty = report.dirty_stamped
    if dirty:
        for treatment in dirty:
            lines.append(
                f'⚠ {treatment.profile}: {treatment.with_markup:,} markup · '
                f'{treatment.with_entities:,} entities · {treatment.with_zero_width:,} zero-width '
                f'survived the treatment — the normaliser is not doing what it claims')
    elif report.stamped:
        lines.append(f'✓ every one of the {report.stamped:,} stamped articles is carrier-free')
    else:
        lines.append('no article carries a treatment stamp yet — nothing has been ingested since '
                     'the normaliser shipped')

    # 3. the flow — what is arriving right now, which the all-time census cannot show
    if report.window_articles:
        share = report.window_stamped / report.window_articles * 100
        lines.append(f'window: {report.window_articles:,} articles fetched, '
                     f'{report.window_stamped:,} stamped ({share:.1f}%)')
    else:
        lines.append(f'window: nothing fetched in the last {report.since_label}')

    # 4. what it removed, within-row
    removal = report.removal
    if removal.rows:
        lines.extend([
            divider,
            f'removed: {removal.chars_removed:,} of {removal.chars_served:,} characters '
            f'({removal.removed_share * 100:.1f}%) across {removal.rows:,} stamped articles',
            f'         {removal.rows_changed:,} of them arrived carrying something '
            f'({removal.changed_share * 100:.1f}%); the rest were already clean',
        ])

    # 5. the keyword fast path — the half that changes signals rather than cost
    lines.append(divider)
    if not report.keyword_sets:
        lines.append('keyword fast path: not checked (no detection vocabulary was supplied)')
        return '\n'.join(lines)
    lines.append(f'keyword fast path, checked against {", ".join(report.keyword_sets)} — '
                 'hits that exist ONLY inside markup')
    if not report.phantoms:
        lines.append('none: every keyword hit in the corpus survives normalising')
    else:
        lines.append(f'{"source":<18} {"set":<14} {"weight":>7} {"gate":>6} {"phantom":>8} '
                     f'{"prose":>6}  flags HIGH alone')
        for phantom in report.phantoms:
            lines.append(
                f'{phantom.source_id:<18.18} {phantom.source_set_id:<14.14} '
                f'{phantom.weight:>7.1f} {phantom.gate:>6.1f} {phantom.phantom_hits:>8,} '
                f'{phantom.prose_hits:>6,}  {"YES" if phantom.self_flags else "no"}')
        if report.phantom_self_flagging:
            lines.append(
                f'⚠ {report.phantom_self_flagging} of {report.phantom_total} phantom hits sit on a '
                f'feed at or above the gate — each one alone raised an article to HIGH')
        for phantom in report.phantoms:
            for example in phantom.examples:
                lines.append(f'  [{phantom.source_id}] …{example.strip()}…')
    if report.orphan_sources:
        lines.append(f'not judged (no configured set claims them): '
                     f'{", ".join(report.orphan_sources)}')
    return '\n'.join(lines)
