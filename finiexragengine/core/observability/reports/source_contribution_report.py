"""Source contribution (ISSUE_82 finding 9) — what each feed actually puts in front of the LLM.

`source_health_report.py` answers *"is this feed reachable"*. `source_latency_report.py` answers
*"how does it behave"*. Neither answers the one that finding 9 stalled on: **is a feed worth what
its configured `source_weight` says?**

That question surfaced when a proposed evidence gate died on it. Half the symbols confirm breaking
on one or two articles, so a `breaking.min_sources` gate looked obvious — until the thin-evidence
articles were joined back to `articles.source_weight` and every one came from a trust-1.0 feed.
That is not a finding: the scale has **two hand-set levels with 10 of 14 feeds at 1.0**, so three
feeds landing on the majority value is what chance produces. The column could not adjudicate its
own question, and `detection.keyword_source_weight` is a binary switch rather than a gradient.

Calibrating the scale needs an *observed* ranking to calibrate against, and none exists. This
builds it, from what the archive already records:

- **articles** — what the feed produced in the window (`articles.source_id`).
- **cited** — how many of those actually reached a prompt, i.e. appeared in at least one
  envelope's `result[].sources[]`. Retrieval selects by similarity and then dedups, so a feed can
  publish heavily and contribute almost nothing.
- **in breaking** — cited by a result the LLM marked `is_breaking`, the passes that matter most.

Deliberately an **observation, not a verdict**: nothing here says a feed *should* weigh more. It
says what the current weights have never been checked against. Read-only and free — no LLM, no
embeddings, one pass over the persisted envelopes.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# Below this many articles in the window a feed has not published enough to be judged: a
# central-bank feed can put out one statement a week, and "0 of 1 cited" says nothing about it.
_JUDGEABLE_ARTICLES = 10
# ...and above it, "low" is measured against the SET's own median citation rate rather than an
# absolute number. Measured 2026-08-19: crypto feeds run 49-89 % and forex 0-96 %, so any fixed
# threshold is right for one set and wrong for the other — the first version of this marker used
# an absolute 10 % calibrated on a stale dev corpus and stayed silent on `cnbc_forex`, which
# published 17 articles and was cited zero times. Half the median is the same idea `SourceLatencyRow`
# uses for its ⚠: the comparison travels with the row so the verdict needs no ambient config.
_LOW_RATE_FACTOR = 0.5


@dataclass
class SourceContributionRow:
    """One feed's contribution inside the window, next to the weight it was assigned by hand."""
    source_id: str
    weight: Optional[float]           # configured source_weight; None = no longer in any config
    articles: int                     # articles this feed produced in the window
    cited: int                        # of those, distinct articles that reached a prompt
    breaking_cited: int               # of those, cited by a result marked is_breaking
    enabled: bool = True
    # The set's median citation rate, carried on the row so a verdict never depends on ambient
    # config — the idiom `SourceLatencyRow.warn_ratio` established.
    median_citation_share: Optional[float] = None

    @property
    def citation_share(self) -> Optional[float]:
        return self.cited / self.articles if self.articles else None

    @property
    def judgeable(self) -> bool:
        """Published enough in the window for its citation rate to mean anything."""
        return self.articles >= _JUDGEABLE_ARTICLES

    @property
    def never_cited(self) -> bool:
        """Published a real number of articles and not one of them reached a prompt."""
        return self.judgeable and self.cited == 0

    @property
    def under_used(self) -> bool:
        """Cited, but far below what the rest of this set achieves."""
        share = self.citation_share
        return (self.judgeable and self.cited > 0 and share is not None
                and self.median_citation_share is not None
                and share < self.median_citation_share * _LOW_RATE_FACTOR)


@dataclass
class SourceContributionReport:
    """One source-set's feeds, most-cited first."""
    source_set_id: str
    since_label: str
    rows: List[SourceContributionRow] = field(default_factory=list)
    envelopes: int = 0                # envelopes walked
    cited_unknown: int = 0            # cited article ids no longer in `articles` (pruned corpus)

    @property
    def articles_total(self) -> int:
        return sum(row.articles for row in self.rows)


def collect_citations(rows: Sequence[Tuple[str, Any]]) -> Tuple[Set[str], Set[str]]:
    """Walk persisted envelopes and return (cited article ids, breaking-cited article ids).

    The DB-free core, so the walking rules are testable without a database — the same split
    `no_data_report._aggregate` uses.

    Walked in Python rather than with jsonb path expressions because `AnalysisEnvelope.result` is
    a list of per-symbol payloads each carrying its own `sources[]`, and `breaking_report.py`
    already reads envelopes exactly this way. A source cited by several symbols in one pass, or
    across many passes, counts **once**: the question is whether the article reached a prompt at
    all, not how often it was reused.
    """
    cited: Set[str] = set()
    breaking: Set[str] = set()
    for _pipeline_id, envelope in rows:
        if not isinstance(envelope, dict):
            continue
        for result in envelope.get('result') or []:
            if not isinstance(result, dict):
                continue
            is_breaking = bool(result.get('is_breaking'))
            for source in result.get('sources') or []:
                article_id = (source or {}).get('article_id')
                if not article_id:
                    continue          # pre-ISSUE_2 shape or a malformed row — skip, never crash
                cited.add(article_id)
                if is_breaking:
                    breaking.add(article_id)
    return cited, breaking


def build_source_contribution_report(
        database_url: str, since: datetime, *, source_set_id: str,
        pipeline_ids: Set[str], weights: Dict[str, float], disabled_ids: Set[str],
        since_label: str = '7d', outcomes_table: str = 'outcomes',
        article_table: str = 'articles') -> SourceContributionReport:
    """Aggregate per-feed contribution for one source-set over the window.

    Args:
        pipeline_ids: the pipelines reading this source-set — a feed's articles can only be cited
            by a pipeline that retrieves over them, so envelopes from other sets are noise here.
        weights: source_id -> configured `source_weight`, from the resolved set.
        disabled_ids: declared but switched off; kept in the table (marked) rather than dropped,
            so a feed that was turned off mid-window does not vanish from its own history.
    """
    report = SourceContributionReport(source_set_id=source_set_id, since_label=since_label)
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            # A database from before the outcome store is a valid empty answer, not a crash.
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (outcomes_table,))
            if cur.fetchone()[0] == 0:
                return report
            cur.execute(
                f'SELECT pipeline_id, envelope FROM {outcomes_table} '
                "WHERE ts >= %s AND status <> 'error' AND pipeline_id = ANY(%s)",
                (since, sorted(pipeline_ids)))
            envelopes = cur.fetchall()
            report.envelopes = len(envelopes)
            cited, breaking = collect_citations(envelopes)

            # What each feed produced in the window.
            cur.execute(
                f'SELECT source_id, count(*) FROM {article_table} '
                'WHERE published_at >= %s GROUP BY source_id', (since,))
            produced: Dict[str, int] = {row[0]: int(row[1]) for row in cur.fetchall()}

            # Map the cited ids back to their feeds. An id the corpus no longer holds is counted
            # separately rather than silently dropped — it means the archive outlived the article.
            cited_by_source: Dict[str, int] = {}
            breaking_by_source: Dict[str, int] = {}
            if cited:
                cur.execute(
                    f'SELECT article_id, source_id FROM {article_table} '
                    'WHERE article_id = ANY(%s)', (sorted(cited),))
                found = cur.fetchall()
                report.cited_unknown = len(cited) - len(found)
                for article_id, source_id in found:
                    cited_by_source[source_id] = cited_by_source.get(source_id, 0) + 1
                    if article_id in breaking:
                        breaking_by_source[source_id] = breaking_by_source.get(source_id, 0) + 1
    except psycopg.Error as exc:
        raise VectorStoreError(f'source contribution report failed: {exc}') from exc

    # Every configured feed gets a row even at zero, so a silent feed is visible as a zero rather
    # than as an absence — the same rule the ingest report follows for its declared catalogue.
    for source_id in sorted(weights):
        report.rows.append(SourceContributionRow(
            source_id=source_id, weight=weights.get(source_id),
            articles=produced.get(source_id, 0),
            cited=cited_by_source.get(source_id, 0),
            breaking_cited=breaking_by_source.get(source_id, 0),
            enabled=source_id not in disabled_ids))
    # The comparison baseline is the set's own median rate over the feeds that published enough
    # to be judged — computed after the rows exist, then handed to each of them.
    median = _median_share(report.rows)
    for row in report.rows:
        row.median_citation_share = median
    report.rows.sort(key=lambda row: (-row.cited, row.source_id))
    return report


def _median_share(rows: Sequence[SourceContributionRow]) -> Optional[float]:
    """Median citation rate across the feeds that published enough for it to mean something."""
    shares = sorted(row.citation_share for row in rows
                    if row.judgeable and row.citation_share is not None)
    if not shares:
        return None
    middle = len(shares) // 2
    return shares[middle] if len(shares) % 2 else (shares[middle - 1] + shares[middle]) / 2


def _share(value: Optional[float]) -> str:
    return f'{value * 100:.1f} %' if value is not None else '    —'


def format_source_contribution_report(report: SourceContributionReport) -> str:
    """Render the shared console pattern (title + window line + dividers + aligned columns)."""
    divider = '-' * 92
    lines = [
        f'Source Contribution — {report.source_set_id} · '
        + (report.since_label if report.since_label == 'all-time'
           else f'last {report.since_label}'),
        f'{report.envelopes} envelopes walked · {report.articles_total} articles in the window',
        divider,
        f'{"source":18} {"weight":>7} {"articles":>9} {"cited":>7} {"citation%":>10} '
        f'{"breaking":>9}  note',
        divider,
    ]
    if not report.rows:
        lines.append('(no feeds configured for this source-set)')
        return '\n'.join(lines + [divider])
    for row in report.rows:
        notes: List[str] = []
        if not row.enabled:
            notes.append('[disabled]')
        if row.never_cited:
            notes.append(f'⚠ published {row.articles}, never cited')
        elif row.under_used:
            notes.append('⚠ far below this set\'s median rate')
        weight = f'{row.weight:.2f}' if row.weight is not None else '   —'
        lines.append(
            f'{row.source_id:18.18} {weight:>7} {row.articles:>9} {row.cited:>7} '
            f'{_share(row.citation_share):>10} {row.breaking_cited:>9}  {" ".join(notes)}'.rstrip())
    lines.append(divider)
    if report.cited_unknown:
        lines.append(f'{report.cited_unknown} cited article(s) no longer in the corpus — '
                     f'the archive outlived them (retention), not an error')
    median = next((row.median_citation_share for row in report.rows
                   if row.median_citation_share is not None), None)
    if median is not None:
        lines.append(f'set median citation rate: {median * 100:.1f} % '
                     f'(feeds under {_JUDGEABLE_ARTICLES} articles are not judged)')
    lines.extend([
        'cited     = distinct articles of this feed that appeared in at least one envelope\'s',
        '            sources[] — retrieval selects by similarity and then dedups, so publishing',
        '            volume and contribution are different quantities',
        'breaking  = of those, cited by a result the LLM marked is_breaking',
        'weight    = the CONFIGURED source_weight. This table is the observation it has never',
        '            been checked against — it ranks feeds, it does not judge them (ISSUE_82 #9)',
    ])
    return '\n'.join(lines)
