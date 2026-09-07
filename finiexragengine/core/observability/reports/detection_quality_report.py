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
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg

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
class DetectionQualityReport:
    since_label: str
    rows: List[TriggerRow] = field(default_factory=list)
    sources: List[SourceRow] = field(default_factory=list)
    examples: List[FlagExample] = field(default_factory=list)
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
                                   articles_table: str = 'articles'
                                   ) -> DetectionQualityReport:
    """Read the flags in the window and fold them into per-path accumulators."""
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # No corpus yet = nothing flagged; a clean empty report, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (articles_table,))
            if not cur.fetchone()[0]:
                return DetectionQualityReport(since_label,
                                              cluster_disabled_sets=list(disabled_sets))
            cur.execute(
                f'SELECT article_id, source_id, title, detection_trigger, importance, '
                f'       cluster_articles, cluster_feeds, flagged_at '
                f'  FROM {articles_table} '
                f' WHERE flagged_at >= %s ORDER BY flagged_at DESC',
                (since,))
            rows = cur.fetchall()
    except psycopg.Error as exc:
        raise VectorStoreError(f'detection-quality report failed: {exc}') from exc

    return aggregate_detection_quality(rows, since_label, example_limit, disabled_sets)


def aggregate_detection_quality(rows: List[Tuple[Any, ...]], since_label: str,
                                example_limit: int = 5,
                                disabled_sets: Sequence[str] = ()) -> DetectionQualityReport:
    """Fold flagged rows into the report — the DB-free core, and the tested one.

    Rows arrive newest first (`ORDER BY flagged_at DESC`), which is also the order the examples
    are taken in: a calibration question is about what the detector is doing *now*, and the oldest
    flags in a window are the ones a threshold change has already superseded.
    """
    report = DetectionQualityReport(since_label, cluster_disabled_sets=list(disabled_sets))
    by_trigger: Dict[str, TriggerRow] = {}
    per_source: Dict[str, int] = {}

    for (article_id, source_id, title, trigger, importance,
         cluster_articles, cluster_feeds, flagged_at) in rows:
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
        if importance is not None and importance >= 3:
            row.high += 1
        else:
            row.mid += 1
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
    return '\n'.join(lines)
