"""Detection sweep (ISSUE_106) — what would each candidate detector have flagged?

Production measured 48 of 48 attributed flags coming from the keyword path and the cluster path
firing **zero** times, at seven feeds and again at eleven, with no MID rows at all. The cause is not
the threshold: the nearest other article inside the 60-minute window sits at cosine distance ~0.56
(median) while the gate is 0.15. And the clusters that *do* form once the gate is loosened are one
feed's daily template — `actionforex`'s "EUR/USD Daily Outlook", "EUR/AUD Daily Outlook", nine pairs
in an hour, distances 0.14–0.25. **Dense embeddings place "same template, different subject" closer
than "same subject, different words"**, which is backwards for corroboration detection.

So the question "would a different detector do better" must be answered before one is deployed, and
this report answers it **by replaying the corpus** rather than by shadowing the live pass:

- no ingest-path change, no migration, no risk to the signal series, no added pass latency;
- it answers a *parameter grid* — a shadow deployment answers only the setting it shipped with;
- it re-runs over any past window, which is what makes the comparison cheap.

**Causal correctness is the one thing a replay can get wrong.** The live detector only ever sees
articles stored *before* the one it is scoring, so every count here is restricted to
`published_at <= seed.published_at`. Without that the numbers would be optimistic by construction.

Three detectors, one grid:

- **`articles`** — `COUNT(*)` of neighbours within the distance, the live semantics;
- **`feeds`** — `COUNT(DISTINCT source_id)` of the same neighbourhood, the design *intent*: the
  difference between the two columns IS the intra-feed duplication;
- **`story`** — the lexical clustering ISSUE_96 already calibrated over 1,455 real texts
  (`assign_stories`), applied one stage earlier to article text instead of to LLM `reasoning`. The
  hypothesis is that IDF weighting discounts a template's shared words and lets the differing entity
  carry — but that is what the sweep TESTS, not a property it may assume: where a feed's body is
  byte-identical across its series, no text measure separates the members, and the grid will say so.

**The story column is a group size, the other two are neighbourhood sizes** — single-link clustering
versus "peers within distance of this seed". Comparable in intent, not identical in construction,
and the render says so rather than letting the reader assume.

No LLM, no embedding calls, read-only: every number comes from the stored corpus.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple

import psycopg

from finiexragengine.core.pipeline.breaking_story_rule import (
    StoryCandidate,
    StoryGrouping,
    assign_stories,
)
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# The similarities the grid walks by default: the live value first, then the loosenings that were
# probed by hand. Deliberately includes 0.85 — a grid whose first row is not the running
# configuration cannot show what changing it would buy.
DEFAULT_SIMILARITIES: Tuple[float, ...] = (0.85, 0.75, 0.65, 0.55)


@dataclass
class SeedNeighbourhood:
    """One sampled article and what surrounds it, per similarity — the sweep's raw row."""
    article_id: str
    source_id: str
    title: str
    published_at: datetime
    # similarity -> (articles within distance, distinct feeds among them)
    embedding_counts: Dict[float, Tuple[int, int]] = field(default_factory=dict)
    # similarity -> size of the lexical story group this article landed in
    story_counts: Dict[float, int] = field(default_factory=dict)


@dataclass
class SweepCell:
    """One (detector, similarity) cell: how many sampled articles would have reached each tier."""
    detector: str          # 'articles' | 'feeds' | 'story'
    similarity: float
    reaches_mid: int
    reaches_high: int


@dataclass
class ClusterExample:
    """The largest neighbourhood at one similarity, with its members named.

    Not decoration: the grid alone said 0.65 "works", and only the titles showed that what fires
    there is one feed's daily template. A sweep that reports counts without evidence invites exactly
    the wrong conclusion.
    """
    similarity: float
    seed_source_id: str
    seed_title: str
    members: int
    distinct_feeds: int
    peers: List[Tuple[str, str, float]] = field(default_factory=list)   # source_id, title, distance

    @property
    def is_single_feed(self) -> bool:
        """One feed carrying its own near-duplicates — the opposite of corroboration."""
        return self.distinct_feeds <= 1


@dataclass
class DetectionSweepReport:
    source_set_id: str
    since_label: str
    seeds: int
    window_minutes: int
    mid_cluster_size: int
    high_cluster_size: int
    live_similarity: float
    cells: List[SweepCell] = field(default_factory=list)
    examples: List[ClusterExample] = field(default_factory=list)
    # Which text treatment the sampled articles carry (ISSUE_112's `text_normalizer` stamp).
    # Load-bearing, not decorative: 44 % of the corpus still stores raw HTML, and two articles
    # sharing a feed's `<p><img class="attachment-post-thumbnail" …>` boilerplate are similar
    # BECAUSE OF THE MARKUP. A sweep over mixed text measures markup similarity as much as story
    # similarity, and would report a false-positive class that #112 has already removed going
    # forward. The stamp exists precisely so this is detectable instead of assumed.
    normalizers: Dict[str, int] = field(default_factory=dict)
    # What the sample actually spans. `--since 30d` asks for a month; a young corpus, or a
    # `--normalizer` filter, can answer with a day. A zero over one day and a zero over a month are
    # different answers, and only the span tells them apart.
    oldest_seed: Optional[datetime] = None
    newest_seed: Optional[datetime] = None

    @property
    def span_hours(self) -> Optional[float]:
        if self.oldest_seed is None or self.newest_seed is None:
            return None
        return (self.newest_seed - self.oldest_seed).total_seconds() / 3600.0

    @property
    def mixed_text_treatments(self) -> bool:
        return len(self.normalizers) > 1

    @property
    def live_cell(self) -> Optional[SweepCell]:
        """The running configuration's own row — what detection actually does today."""
        return next((c for c in self.cells
                     if c.detector == 'articles' and c.similarity == self.live_similarity), None)

    @property
    def boilerplate_examples(self) -> int:
        """How many of the shown clusters are one feed duplicating itself."""
        return sum(1 for example in self.examples if example.is_single_feed)


def _seed_rows(cur: psycopg.Cursor, table: str, since: datetime, source_ids: Set[str],
               sample: int, normalizer: Optional[str]) -> List[tuple]:
    """The sampled seeds, carrying their `text_normalizer` stamp.

    `normalizer` narrows the sample to one text treatment — the only way to compare like with like
    while the corpus holds both. `''` selects the un-normalised rows; None takes whatever is there
    and lets the render report the mix.
    """
    where = ['embedding IS NOT NULL', 'published_at >= %s', 'source_id = ANY(%s)']
    params: List[object] = [since, sorted(source_ids)]
    if normalizer is not None:
        where.append('coalesce(text_normalizer, %s) = %s')
        params += ['', normalizer]
    params.append(sample)
    cur.execute(
        f'SELECT article_id, source_id, title, coalesce(summary, %s), published_at, '
        f"coalesce(text_normalizer, '') FROM {table} WHERE " + ' AND '.join(where)
        + ' ORDER BY published_at DESC LIMIT %s', ['', *params])
    return cur.fetchall()


def _embedding_counts(cur: psycopg.Cursor, table: str, similarities: Sequence[float],
                      window_minutes: int, source_ids: Set[str],
                      seed_ids: List[str]) -> Dict[str, Dict[float, Tuple[int, int]]]:
    """Per seed and similarity: neighbours within the distance, and how many feeds they span.

    One query over all thresholds — the same shape `count_neighbors` uses, aggregated with FILTER so
    the corpus is walked once instead of once per grid row. `a.published_at <= s.published_at` is
    the causal restriction; without it a later article would count as a neighbour of an earlier one.
    """
    filters = ', '.join(
        f'count(*) FILTER (WHERE (a.embedding <=> s.embedding) <= {1.0 - sim:.4f}) AS n{index}, '
        f'count(DISTINCT a.source_id) FILTER '
        f'(WHERE (a.embedding <=> s.embedding) <= {1.0 - sim:.4f}) AS f{index}'
        for index, sim in enumerate(similarities))
    cur.execute(
        f'SELECT s.article_id, {filters} FROM {table} s JOIN {table} a '
        f'  ON a.published_at >= s.published_at - make_interval(mins => %s) '
        f' AND a.published_at <= s.published_at '
        f' AND a.source_id = ANY(%s) '
        f'WHERE s.article_id = ANY(%s) GROUP BY s.article_id',
        (window_minutes, sorted(source_ids), seed_ids))
    out: Dict[str, Dict[float, Tuple[int, int]]] = {}
    for row in cur.fetchall():
        out[row[0]] = {sim: (int(row[1 + index * 2]), int(row[2 + index * 2]))
                       for index, sim in enumerate(similarities)}
    return out


def _story_counts(window_rows: List[tuple], seed_ids: Set[str],
                  similarities: Sequence[float],
                  window_minutes: int) -> Dict[str, Dict[float, int]]:
    """Per seed and similarity: the size of the lexical story group it lands in.

    Uses ISSUE_96's `assign_stories` through its public interface rather than reimplementing the
    measure — one clustering, two callers, which is the divergence ISSUE_82 removed one domain over.
    `key=''` puts every article in one analysis unit: an article has no symbol to be keyed by, and
    the point of the measure here is precisely to cluster *across* subjects.
    """
    counts: Dict[str, Dict[float, int]] = {aid: {} for aid, *_ in window_rows if aid in seed_ids}
    for similarity in similarities:
        grouping = StoryGrouping(similarity=similarity,
                                 window=timedelta(minutes=window_minutes))
        candidates = [StoryCandidate(key='', started=published, reason=f'{title}. {summary}')
                      for _aid, _sid, title, summary, published in window_rows]
        story_ids = assign_stories(candidates, grouping)
        sizes: Dict[int, int] = {}
        for story_id in story_ids:
            sizes[story_id] = sizes.get(story_id, 0) + 1
        for (article_id, *_), story_id in zip(window_rows, story_ids):
            if article_id in seed_ids:
                counts[article_id][similarity] = sizes[story_id]
    return counts


def _example_for(cur: psycopg.Cursor, table: str, seed: SeedNeighbourhood,
                 similarity: float, window_minutes: int,
                 source_ids: Set[str]) -> ClusterExample:
    """Name the members of one neighbourhood — the evidence the grid cannot carry."""
    members, feeds = seed.embedding_counts.get(similarity, (0, 0))
    cur.execute(
        f'SELECT a.source_id, a.title, round((a.embedding <=> s.embedding)::numeric, 3) '
        f'FROM {table} s JOIN {table} a '
        f'  ON a.published_at >= s.published_at - make_interval(mins => %s) '
        f' AND a.published_at <= s.published_at AND a.source_id = ANY(%s) '
        f'WHERE s.article_id = %s AND a.article_id <> s.article_id '
        f'  AND (a.embedding <=> s.embedding) <= %s ORDER BY 3 LIMIT 4',
        (window_minutes, sorted(source_ids), seed.article_id, 1.0 - similarity))
    peers = [(sid, title, float(distance)) for sid, title, distance in cur.fetchall()]
    return ClusterExample(similarity=similarity, seed_source_id=seed.source_id,
                          seed_title=seed.title, members=members,
                          distinct_feeds=feeds, peers=peers)


def build_detection_sweep_report(database_url: str, since: datetime, *,
                                 source_set_id: str, source_ids: Set[str],
                                 window_minutes: int, mid_cluster_size: int,
                                 high_cluster_size: int, live_similarity: float,
                                 since_label: str = '7d', sample: int = 400,
                                 normalizer: Optional[str] = None,
                                 similarities: Sequence[float] = DEFAULT_SIMILARITIES,
                                 articles_table: str = 'articles') -> DetectionSweepReport:
    """Replay the corpus and score every detector at every similarity in the grid."""
    report = DetectionSweepReport(
        source_set_id=source_set_id, since_label=since_label, seeds=0,
        window_minutes=window_minutes, mid_cluster_size=mid_cluster_size,
        high_cluster_size=high_cluster_size, live_similarity=live_similarity)
    if not source_ids:
        return report
    try:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM information_schema.tables WHERE table_name = %s',
                        (articles_table,))
            if not cur.fetchone()[0]:
                return report               # no corpus yet — an empty sweep, not a crash
            seed_rows = _seed_rows(cur, articles_table, since, source_ids, sample, normalizer)
            if not seed_rows:
                return report
            seeds = [SeedNeighbourhood(article_id=aid, source_id=sid, title=title,
                                       published_at=published)
                     for aid, sid, title, _summary, published, _norm in seed_rows]
            report.oldest_seed = min(seed.published_at for seed in seeds)
            report.newest_seed = max(seed.published_at for seed in seeds)
            for *_rest, norm in seed_rows:
                key = norm or '(raw — pre-ISSUE_112)'
                report.normalizers[key] = report.normalizers.get(key, 0) + 1
            embedding = _embedding_counts(cur, articles_table, similarities, window_minutes,
                                          source_ids, [s.article_id for s in seeds])
            for seed in seeds:
                seed.embedding_counts = embedding.get(seed.article_id, {})

            # The story half needs the texts of the whole window, not only the seeds: document
            # frequency is what suppresses boilerplate, and a seeds-only corpus is too small to
            # see it (the same reasoning `_vectors` states for episodes).
            oldest = min(seed.published_at for seed in seeds)
            cur.execute(
                f'SELECT article_id, source_id, title, coalesce(summary, %s), published_at '
                f'FROM {articles_table} '
                f'WHERE published_at >= %s AND source_id = ANY(%s) ORDER BY published_at',
                ('', oldest - timedelta(minutes=window_minutes), sorted(source_ids)))
            window_rows = cur.fetchall()
            seed_ids = {seed.article_id for seed in seeds}
            story = _story_counts(window_rows, seed_ids, similarities, window_minutes)
            for seed in seeds:
                seed.story_counts = story.get(seed.article_id, {})

            # One example per similarity: the largest neighbourhood, named.
            for similarity in similarities:
                ranked = sorted(seeds, key=lambda s: -s.embedding_counts.get(similarity, (0, 0))[0])
                if ranked and ranked[0].embedding_counts.get(similarity, (0, 0))[0] >= 2:
                    report.examples.append(_example_for(cur, articles_table, ranked[0],
                                                        similarity, window_minutes, source_ids))
    except psycopg.Error as exc:
        raise VectorStoreError(f'detection sweep failed: {exc}') from exc

    report.seeds = len(seeds)
    for similarity in similarities:
        articles = [s.embedding_counts.get(similarity, (0, 0))[0] for s in seeds]
        feeds = [s.embedding_counts.get(similarity, (0, 0))[1] for s in seeds]
        stories = [s.story_counts.get(similarity, 0) for s in seeds]
        for detector, values in (('articles', articles), ('feeds', feeds), ('story', stories)):
            report.cells.append(SweepCell(
                detector=detector, similarity=similarity,
                reaches_mid=sum(1 for v in values if v >= mid_cluster_size),
                reaches_high=sum(1 for v in values if v >= high_cluster_size)))
    return report


def format_detection_sweep_report(report: DetectionSweepReport) -> str:
    """Render the grid + the evidence, in the shared console pattern."""
    divider = '-' * 86
    lines = [
        'Detection Sweep — what each detector would have flagged',
        f'window: last {report.since_label} · {report.seeds} seeds · {report.source_set_id} · '
        f'cluster window {report.window_minutes}min · '
        f'tiers MID>={report.mid_cluster_size} HIGH>={report.high_cluster_size}',
        'replayed from the corpus — neighbours restricted to published_at <= the seed, so the '
        'counts are what the live detector could have seen',
        divider,
    ]
    span = report.span_hours
    if span is not None:
        thin = span < 72
        note = ('  ⚠️  a THIN sample: a zero here says "not in these hours", not "does not happen"'
                if thin else '')
        lines.append(f'sample spans {span / 24:.1f} days '
                     f'({report.oldest_seed:%Y-%m-%d %H:%M} … {report.newest_seed:%Y-%m-%d %H:%M})'
                     f'{note}')
    if report.normalizers:
        mix = ' · '.join(f'{name} {count}' for name, count in sorted(report.normalizers.items()))
        lines.append(f'text treatment of the sample: {mix}')
        if report.mixed_text_treatments:
            lines.append('⚠️  MIXED text treatments — raw rows still carry the feed\'s HTML '
                         'boilerplate, and two articles sharing a template\'s markup are similar '
                         'BECAUSE OF THE MARKUP. Re-run with one treatment (--normalizer) before '
                         'reading a similarity off this grid.')
        lines.append(divider)
    if not report.seeds:
        lines.append('(no articles in the window for this source set)')
        return '\n'.join(lines)

    lines.append(f'{"similarity":>10} | {"ARTICLES >=mid":>14} {">=high":>7} | '
                 f'{"FEEDS >=mid":>11} {">=high":>7} | {"STORY >=mid":>11} {">=high":>7}')
    lines.append(divider)
    by_key = {(c.detector, c.similarity): c for c in report.cells}
    for similarity in sorted({c.similarity for c in report.cells}, reverse=True):
        cells = [by_key.get((d, similarity)) for d in ('articles', 'feeds', 'story')]
        marker = '  <- live' if similarity == report.live_similarity else ''
        values = ' | '.join(f'{c.reaches_mid:>11}/{report.seeds:<4} {c.reaches_high:>6}'
                            if c else f'{"—":>11} {"—":>6}' for c in cells)
        lines.append(f'{similarity:>10.2f} | {values}{marker}')
    lines.append(divider)

    live = report.live_cell
    if live is not None and live.reaches_mid == 0:
        lines.append(f'the running configuration ({report.live_similarity:.2f}, counting ARTICLES) '
                     f'reaches neither tier on any of the {report.seeds} seeds — the cluster path '
                     f'is inert, not merely strict')
    lines.append('ARTICLES vs FEEDS: the gap between the two columns IS intra-feed duplication — '
                 'one feed carrying its own near-duplicates, which corroborates nothing')
    lines.append('STORY is a group size (single-link over article text), the other two are '
                 'neighbourhood sizes — comparable in intent, not identical in construction')

    if report.examples:
        lines += ['', 'largest neighbourhood per similarity — the evidence the grid cannot carry:']
        for example in report.examples:
            verdict = ('  ← ONE FEED, one template: intra-feed duplication, not corroboration'
                       if example.is_single_feed else '')
            lines.append(f'\n  similarity {example.similarity:.2f} · {example.members} members · '
                         f'{example.distinct_feeds} distinct feed(s){verdict}')
            lines.append(f'    SEED [{example.seed_source_id}] {example.seed_title[:66]}')
            for source_id, title, distance in example.peers:
                lines.append(f'      d={distance:.3f}  [{source_id:14.14}] {title[:58]}')
        if report.boilerplate_examples:
            lines.append(f'\n{report.boilerplate_examples} of {len(report.examples)} shown '
                         f'neighbourhoods are a single feed. Loosening the similarity alone admits '
                         f'those first — before it admits any cross-feed story.')
    return '\n'.join(lines)
