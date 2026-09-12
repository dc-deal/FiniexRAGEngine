"""Detection quality (ISSUE_106) — what the detector flagged, and whether the evidence holds.

`breaking` reports the **outcome**: confirmed episodes, reaction times, episodes versus stories.
This reports the detector's **own behaviour** one stage earlier — which path fired, on what
neighbourhood, and whether that neighbourhood was corroboration or one feed talking to itself.

It is the archive counterpart to `detection_sweep`. The sweep replays the corpus and answers *what
a setting would do*; this reads what the running one *did*, from the counts the detector recorded at
flag time (migration 013). Two questions, deliberately two reports: a replay cannot be wrong about
the past, and a record cannot be wrong about the present — and until the counts were stored, the
only way to ask the second question was to run the first.

**The duplication ratio is the report's statement.** `cluster_articles / cluster_feeds` is near 1.0
when distinct outlets carried one story, and 3x or more when a single feed supplied most of the
neighbourhood it was credited for. That number is why both counts are stored rather than one: it is
invisible in either alone, and it is the difference between the cluster path working and the failure
mode that kept it switched off — `actionforex` publishing nine currency-pair outlooks in an hour and
reaching a cluster of nine by itself.

**Named examples, because a grid is not evidence.** On 2026-09-01 the sweep's numbers said 0.65
"works" and only the neighbourhood titles showed it firing on a daily template. The same rule holds
for the live path: the ratio flags a suspicion, the headline settles it.

Read-only over persisted corpus columns — no embeddings, no LLM, no paid call, which is what puts it
on the report catalog rather than beside `coverage`.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import psycopg

from finiexragengine.core.observability.reports.corpus_text_report import KeywordSet
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# Above this, a flagged neighbourhood was mostly one feed repeating itself. Not a hard verdict — the
# report marks it and names the story rather than judging — but the number has an origin: measured
# 2026-09-07, genuine multi-outlet neighbourhoods ran 1.0–1.4 articles per feed while the
# `actionforex` template ran 9 articles on 1 feed.
_DUPLICATION_SUSPECT = 2.0


@dataclass
class TriggerRow:
    """One detection path's behaviour over the window."""
    trigger: str
    flags: int = 0
    mid: int = 0
    high: int = 0
    # Only the cluster path records a neighbourhood; a keyword flag consulted none, so these stay
    # empty and every derived value below reads as unavailable rather than as zero.
    feeds: List[int] = field(default_factory=list)
    articles: List[int] = field(default_factory=list)

    @property
    def measured(self) -> int:
        """Flags that carry a recorded neighbourhood — the population the numbers below describe."""
        return len(self.feeds)

    @property
    def feeds_min(self) -> Optional[int]:
        return min(self.feeds) if self.feeds else None

    @property
    def feeds_median(self) -> Optional[float]:
        return _median(self.feeds)

    @property
    def feeds_max(self) -> Optional[int]:
        return max(self.feeds) if self.feeds else None

    @property
    def articles_median(self) -> Optional[float]:
        return _median(self.articles)

    @property
    def duplication(self) -> Optional[float]:
        """Articles per distinct feed, pooled over the window rather than averaged per flag.

        Pooled on purpose: one flag with 9 articles on 1 feed and one with 3 on 3 is a mixed
        window, and averaging the two ratios (9.0 and 1.0 -> 5.0) would describe neither. Summing
        both sides gives 12/4 = 3.0, which is what the window actually delivered.
        """
        total_feeds = sum(self.feeds)
        return sum(self.articles) / total_feeds if total_feeds else None

    @property
    def suspect(self) -> bool:
        ratio = self.duplication
        return ratio is not None and ratio >= _DUPLICATION_SUSPECT


@dataclass
class FlagExample:
    """One recent cluster flag, named — the evidence a ratio cannot carry."""
    article_id: str
    source_id: str
    title: str
    importance: int
    cluster_articles: int
    cluster_feeds: int
    flagged_at: Optional[datetime]

    @property
    def duplication(self) -> Optional[float]:
        return self.cluster_articles / self.cluster_feeds if self.cluster_feeds else None

    @property
    def single_feed(self) -> bool:
        """The shape that must never reach a flag once a set counts distinct feeds."""
        return self.cluster_feeds <= 1


@dataclass
class SourceRow:
    """How many cluster flags one feed's articles received."""
    source_id: str
    flags: int


@dataclass
class TermRow:
    """One vocabulary term's behaviour over the window (migration 014).

    The keyword path's answer to the duplication ratio. `duplication` asks whether a cluster was
    corroboration or one feed repeating itself; the equivalent question for a keyword is whether a
    term is *vocabulary* or one publisher's house style — a term firing only on `sec_press` is a
    property of that feed's template, not a crisis signal, and #46 measured exactly that (the bare
    token `SEC` fires on 25 of 25 SEC press releases, which is why `sec_press` runs parked at 0.8).
    """
    term: str
    flags: int = 0
    high: int = 0
    mid: int = 0
    # Distinct feeds this term ever fired on — a set, because the same feed firing it forty times
    # is one publisher, and counting rows would hide that behind a large number.
    feeds: Set[str] = field(default_factory=set)

    @property
    def feed_count(self) -> int:
        return len(self.feeds)

    @property
    def single_feed(self) -> bool:
        """Fired, but never outside one publisher — marked, deliberately not judged.

        Same treatment as `_DUPLICATION_SUSPECT`: the marker raises the suspicion and the operator
        reads the feed name. A term can legitimately be single-feed (a central bank is the only
        publisher of its own decision), so this must never read as a verdict.
        """
        return self.flags > 0 and len(self.feeds) == 1

    @property
    def only_feed(self) -> Optional[str]:
        return next(iter(self.feeds)) if len(self.feeds) == 1 else None


@dataclass
class SilentTerm:
    """A configured term the window recorded no flag for (migration 014).

    Named with its source set, because the vocabularies are per set and a forex term reading
    "never fired" over a crypto-heavy window would otherwise look like a defect. Silence is not a
    verdict either: a crisis word *should* be silent in a calm week. It is reported because the
    alternative — a term that is silent because it can never match — is invisible without it, and
    that is the failure `monetary policy decision` vs `Monetary policy decisions` produces.
    """
    source_set_id: str
    term: str


@dataclass
class DetectionQualityReport:
    since_label: str
    rows: List[TriggerRow] = field(default_factory=list)
    sources: List[SourceRow] = field(default_factory=list)
    examples: List[FlagExample] = field(default_factory=list)
    # The keyword path's evidence (migration 014) — what the cluster path has had since 013.
    terms: List[TermRow] = field(default_factory=list)
    silent_terms: List[SilentTerm] = field(default_factory=list)
    # How many terms each set declares, so silence can be reported as a SHARE. "10 of 10 silent"
    # is a different statement from "10 silent" when nobody knows the denominator, and it is the
    # one that says a whole vocabulary contributed nothing.
    vocabulary_size: Dict[str, int] = field(default_factory=dict)
    # Keyword flags carrying no recorded vocabulary: made before the column existed. Counted
    # separately from `unattributed` above, which is about the *path* rather than the term — a
    # flag can know which path fired it and not which word did.
    terms_unrecorded: int = 0
    # Sets whose cluster path is switched off, so an empty cluster row reads as a decision rather
    # than as a threshold somebody should go fix (ISSUE_106).
    cluster_disabled_sets: List[str] = field(default_factory=list)
    unattributed: int = 0        # flags from before the trigger column existed

    @property
    def total_flags(self) -> int:
        return sum(row.flags for row in self.rows) + self.unattributed

    @property
    def cluster_row(self) -> Optional[TriggerRow]:
        return next((row for row in self.rows if row.trigger == 'cluster'), None)


def _median(values: Sequence[int]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def build_detection_quality_report(database_url: str, since: datetime, *,
                                   since_label: str = '7d', example_limit: int = 5,
                                   disabled_sets: Sequence[str] = (),
                                   keyword_sets: Sequence[KeywordSet] = (),
                                   articles_table: str = 'articles'
                                   ) -> DetectionQualityReport:
    """Read the flags in the window and fold them into per-path accumulators.

    `keyword_sets` carries the configured vocabulary, which the store cannot supply: a term that
    never fired leaves no row, so "declared and silent" is only visible by comparing the flags
    against the config. Carried in rather than resolved here for the reason every other report
    takes its config in — the registry factories are the only load path that honours the
    `user_configs/` overlay, and a report resolving its own would describe a configuration that
    did not run.
    """
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # No corpus yet = nothing flagged; a clean empty report, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (articles_table,))
            if not cur.fetchone()[0]:
                return DetectionQualityReport(
                    since_label, cluster_disabled_sets=list(disabled_sets),
                    silent_terms=_silent_terms({}, keyword_sets),
                    vocabulary_size={ks.source_set_id: len(ks.keywords)
                                     for ks in keyword_sets if ks.keywords})
            cur.execute(
                f'SELECT article_id, source_id, title, detection_trigger, importance, '
                f'       cluster_articles, cluster_feeds, flagged_at, detection_keywords '
                f'  FROM {articles_table} '
                f' WHERE flagged_at >= %s ORDER BY flagged_at DESC',
                (since,))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise VectorStoreError(f'detection-quality report failed: {exc}') from exc

    return aggregate_detection_quality(rows, since_label, example_limit, disabled_sets,
                                       keyword_sets)


def _silent_terms(fired: Dict[str, TermRow],
                  keyword_sets: Sequence[KeywordSet]) -> List[SilentTerm]:
    """Configured terms with no flag in the window, named with the set that declares them."""
    return sorted(
        (SilentTerm(source_set_id=keyword_set.source_set_id, term=term)
         for keyword_set in keyword_sets
         for term in keyword_set.keywords
         if term not in fired),
        key=lambda row: (row.source_set_id, row.term))


def aggregate_detection_quality(rows: List[Tuple[Any, ...]], since_label: str,
                                example_limit: int = 5,
                                disabled_sets: Sequence[str] = (),
                                keyword_sets: Sequence[KeywordSet] = ()
                                ) -> DetectionQualityReport:
    """Fold flagged rows into the report — the DB-free core, and the tested one.

    Rows arrive newest first (`ORDER BY flagged_at DESC`), which is also the order the examples
    are taken in: a calibration question is about what the detector is doing *now*, and the oldest
    flags in a window are the ones a threshold change has already superseded.
    """
    report = DetectionQualityReport(since_label, cluster_disabled_sets=list(disabled_sets))
    by_trigger: Dict[str, TriggerRow] = {}
    per_source: Dict[str, int] = {}
    by_term: Dict[str, TermRow] = {}

    for (article_id, source_id, title, trigger, importance,
         cluster_articles, cluster_feeds, flagged_at, keywords) in rows:
        if not trigger:
            # Flagged before migration 011 existed. An absence, never a category — folding it into
            # a path would invent evidence for whichever one is being judged.
            report.unattributed += 1
            continue
        row = by_trigger.get(trigger)
        if row is None:
            row = TriggerRow(trigger=trigger)
            by_trigger[trigger] = row
        row.flags += 1
        high = importance is not None and importance >= 3
        if high:
            row.high += 1
        else:
            row.mid += 1
        # The keyword path's evidence, before the neighbourhood check below: a keyword flag has no
        # neighbourhood by construction, so folding terms in after that `continue` would record
        # nothing at all — the exact shape of the defect this column closes.
        if trigger == 'keyword':
            if not keywords:
                # Flagged before migration 014. An absence, never "matched no term" — a keyword
                # flag by definition matched one, and we simply did not write it down.
                report.terms_unrecorded += 1
            for term in keywords or ():
                term_row = by_term.get(term)
                if term_row is None:
                    term_row = TermRow(term=term)
                    by_term[term] = term_row
                term_row.flags += 1
                term_row.high += int(high)
                term_row.mid += int(not high)
                term_row.feeds.add(source_id)
        if cluster_feeds is None or cluster_articles is None:
            continue                    # no neighbourhood was consulted for this flag
        row.feeds.append(int(cluster_feeds))
        row.articles.append(int(cluster_articles))
        per_source[source_id] = per_source.get(source_id, 0) + 1
        if len(report.examples) < example_limit:
            report.examples.append(FlagExample(
                article_id=article_id, source_id=source_id, title=title,
                importance=int(importance) if importance is not None else 0,
                cluster_articles=int(cluster_articles), cluster_feeds=int(cluster_feeds),
                flagged_at=flagged_at))

    report.rows = sorted(by_trigger.values(), key=lambda row: (-row.flags, row.trigger))
    report.sources = sorted((SourceRow(source_id=sid, flags=count)
                             for sid, count in per_source.items()),
                            key=lambda row: (-row.flags, row.source_id))
    report.terms = sorted(by_term.values(), key=lambda row: (-row.flags, row.term))
    report.silent_terms = _silent_terms(by_term, keyword_sets)
    report.vocabulary_size = {keyword_set.source_set_id: len(keyword_set.keywords)
                              for keyword_set in keyword_sets if keyword_set.keywords}
    return report


def _num(value: Optional[float], places: int = 1) -> str:
    return f'{value:.{places}f}' if value is not None else '—'


def format_detection_quality_report(report: DetectionQualityReport) -> str:
    """The shared console pattern: title, window line, `----` dividers, aligned columns."""
    lines = ['Detection Quality — what the detector flagged, and on what evidence',
             f'window: last {report.since_label} · {report.total_flags} flags'
             + (f' · {report.unattributed} from before the trigger column existed'
                if report.unattributed else ''),
             '-' * 86]
    for source_set_id in report.cluster_disabled_sets:
        lines.append(f'{source_set_id} · cluster path OFF by config — its flags are keyword-only, '
                     f'and an empty cluster row here is a decision, not a gap')
    if report.cluster_disabled_sets:
        lines.append('-' * 86)

    if not report.rows:
        lines.append('nothing was flagged in this window — neither path fired')
        # The vocabulary section still renders: a window where NOTHING fired is exactly when
        # "which of my terms is silent" is the question, and returning here would suppress the
        # answer precisely in the case that prompts it.
        lines.extend(_term_lines(report))
        return '\n'.join(lines)

    lines.append(f'{"trigger":10s} {"flags":>6s} {"MID":>5s} {"HIGH":>5s} | '
                 f'{"feeds min/med/max":>18s} | {"articles med":>12s} | {"duplication":>12s}')
    for row in report.rows:
        if row.measured:
            spread = (f'{row.feeds_min} / {_num(row.feeds_median)} / {row.feeds_max}')
            ratio = f'{_num(row.duplication, 2)}x' + ('  ⚠' if row.suspect else '')
        else:
            # The keyword path consulted no neighbourhood. `—` says "not measured"; a 0 here would
            # claim an empty cluster was looked at, which is the distinction NULL exists to keep.
            spread, ratio = '—', '—'
        lines.append(f'{row.trigger:10s} {row.flags:6d} {row.mid:5d} {row.high:5d} | '
                     f'{spread:>18s} | {_num(row.articles_median):>12s} | {ratio:>12s}')
    lines.append('-' * 86)
    lines.append('duplication = articles ÷ distinct feeds across the flagged neighbourhoods, '
                 'pooled over the window.')
    lines.append(f'~1.0 is a story carried by separate outlets; {_DUPLICATION_SUSPECT:.0f}x and '
                 'above means one feed supplied most of what it was credited for')

    if report.sources:
        lines.append('-' * 86)
        lines.append('cluster flags per feed — one feed dominating is the shape to look at')
        for row in report.sources[:8]:
            lines.append(f'  {row.source_id:22.22s} {row.flags:4d}')

    if report.examples:
        lines.append('-' * 86)
        lines.append('recent cluster flags, newest first — the ratio flags a suspicion, the '
                     'headline settles it')
        for example in report.examples:
            mark = '  ⚠ SINGLE FEED' if example.single_feed else ''
            lines.append(f'  [{example.source_id:14.14s}] {example.title[:44]:44.44s} '
                         f'{example.cluster_feeds} feeds / {example.cluster_articles} art '
                         f'({_num(example.duplication, 1)}x){mark}')

    lines.extend(_term_lines(report))
    return '\n'.join(lines)


def _term_lines(report: DetectionQualityReport) -> List[str]:
    """The keyword path's per-term breakdown (migration 014) — what a vocabulary decision needs.

    Rendered only when there is something to say. The section is deliberately separate from the
    trigger table above: that one compares the two *paths*, this one compares the terms *inside*
    one path, and folding them together would put two different populations in one column.
    """
    if not report.terms and not report.silent_terms and not report.terms_unrecorded:
        return []
    lines = ['-' * 86,
             'keyword vocabulary — which term fired, and whether it is vocabulary or one '
             'publisher\'s template']
    if report.terms_unrecorded:
        # Same distinction the `unattributed` count draws one level up: these flags know their
        # path and not their word, because they predate the column.
        lines.append(f'{report.terms_unrecorded} keyword flag(s) carry no recorded term — '
                     f'flagged before the column existed, not "matched nothing"')
    if report.terms:
        # No MID/HIGH columns: `_tier` returns HIGH for every keyword verdict, so those two would
        # be a constant 0 and a copy of `flags` — three columns carrying one number, and two of
        # them reading as measurements. The fact itself is worth stating once, below.
        lines.append(f'{"term":26s} {"flags":>6s} {"feeds":>6s}')
        for row in report.terms:
            mark = f'  ⚠ only {row.only_feed}' if row.single_feed else ''
            lines.append(f'{row.term:26.26s} {row.flags:6d} {row.feed_count:6d}{mark}')
        lines.append('every keyword flag is HIGH by construction (`_tier`), so each row above is '
                     'a breaking wake — this path is what takes the engine off its cadence')
    if report.silent_terms:
        # Silence is reported, never judged: a crisis word SHOULD be quiet in a calm week. What it
        # makes visible is the other kind — a term that cannot match at all, which is otherwise
        # indistinguishable from one whose event simply has not happened.
        lines.extend(_silence_lines(report))
    return lines


# How many silent terms one set names before the line collapses to `+N more` — per SET, not per
# report. Pooled, the first set's vocabulary fills the cap and every later set vanishes behind the
# counter: sorted by (set, term), eight names meant `crypto_news` only and `forex_news` was
# invisible. Same `+N more` idiom as the `[OVERRIDE]` and preflight lines.
_NAMED_SILENT = 6


def _silence_lines(report: DetectionQualityReport) -> List[str]:
    """Configured terms with no flag, grouped per source set and reported as a share."""
    by_set: Dict[str, List[str]] = {}
    for row in report.silent_terms:
        by_set.setdefault(row.source_set_id, []).append(row.term)
    lines: List[str] = []
    for source_set_id, terms in sorted(by_set.items()):
        declared = report.vocabulary_size.get(source_set_id, len(terms))
        shown = ', '.join(terms[:_NAMED_SILENT])
        rest = len(terms) - min(len(terms), _NAMED_SILENT)
        # "10 of 10" is the statement; a bare count cannot say a WHOLE vocabulary contributed
        # nothing, which is the reading that should prompt a look at the feeds.
        lines.append(f'{source_set_id} · {len(terms)} of {declared} configured term(s) silent: '
                     f'{shown}' + (f' +{rest} more' if rest else ''))
    if report.terms_unrecorded and not report.terms:
        # The claim names its population, like `_count_label` and `measured` elsewhere. Without
        # this the transition reads as a vocabulary collapse: every term looks silent while the
        # column is simply not populated for the flags this window holds.
        lines.append('⚠ every keyword flag in this window predates the term column, so NOTHING '
                     'could be attributed — this silence is the migration, not the vocabulary')
    else:
        lines.append('a quiet window explains silence; a term that can never match looks '
                     'identical here, so check the spelling against a feed before trusting it')
    return lines
