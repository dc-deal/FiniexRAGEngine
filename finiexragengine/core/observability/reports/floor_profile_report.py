"""Retrieval floor profile (ISSUE_55 groundwork) — is the floor discriminating, per query?

`coverage_report.py` answers *"does the corpus cover this symbol"*. This answers the question
underneath it: **`retrieval.floor_distance` is one absolute cut applied to per-query distance
distributions that are not comparable**, so the same number is simultaneously too strict for one
query and too loose for another — and both failures are silent.

Measured 2026-08-19 on the live archive: ADAUSD (`'Cardano ADA'`) lost **410 of 1075 passes** to a
floor of 0.700 with its nearest candidate at 0.706-0.710 — six to ten thousandths. Meanwhile
DASHUSD (`'Dash cryptocurrency'`), by far the smallest coin configured, never starved once. Its
query is the only one carrying a generic anchor word, which is a plausible reason for it to sit
close to *any* crypto article rather than to Dash news.

That second direction is the one #55 does not consider: it reasons throughout about a floor that is
too high and starves a symbol. A floor that is too *low* for a generic query feeds a symbol
articles that are not about it — exactly what ISSUE_24 exists to prevent, defeated for one row
without anything saying so.

Both verdicts here are therefore **threshold-free**, because the distributions they judge have
never been looked at and inventing a constant now would only hide that:

- **starved** - the nearest article in the window is already beyond the floor, so the query is
  scored on nothing at all. No judgement needed; it either is or is not.
- **indiscriminate** - the *median* of the whole window is inside the floor, i.e. more than half
  the corpus counts as relevant to one symbol. For a symbol-specific query that is implausible on
  its face, and the `foreign` column says how much of it comes from another pipeline's feeds.

The knee (largest gap in the nearest-N distance curve) is #55 step 7's *"deterministic
gap-detection baseline ... computed alongside as a cross-check"*, built here ahead of the LLM half
so the cross-check exists before there is anything to cross-check against.

Read-only and free: query vectors come from the cache (ISSUE_19), so a run makes no API call on a
hit, and everything else is distance arithmetic inside the database.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import psycopg
from pgvector.psycopg import register_vector

from finiexragengine.core.observability.reports.no_data_report import NoDataRow
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# How many of the nearest articles the knee is computed over. Matches ISSUE_55's sample cap, so
# the deterministic baseline and the LLM-judged cut end up describing the same slice of the curve.
_KNEE_SAMPLE = 200

# (passes, no_data_passes, nearest miss, mean miss) — what the archive contributes per symbol.
_Archive = Tuple[int, int, Optional[float], Optional[float]]


@dataclass
class FloorProfileRow:
    """One query's distance profile against the live window, plus how it fared in the archive.

    The unit is the **query text**, not the symbol: the floor is applied to a query, and several
    symbols can share one (ETHUSD + ETHEUR both retrieve on 'Ethereum ETH').
    """
    query_text: str
    symbols: List[str]
    window_articles: int                  # articles the window held when measured
    nearest: Optional[float]              # min distance; None on an empty window
    p10: Optional[float]
    median: Optional[float]
    in_floor: int                         # window articles within the floor (BEFORE dedup/top_k)
    foreign_in_floor: int                 # of those, from a feed outside this pipeline's own set
    knee: Optional[float]                 # largest gap in the nearest-N curve -> floor candidate
    floor: float
    # From the persisted archive, not the live corpus: what this query's symbols actually did.
    archive_passes: int = 0
    archive_no_data: int = 0
    # ...and how close the corpus came on the passes that produced NOTHING. This is the number the
    # live columns cannot show: starvation is episodic, so a snapshot taken on a good minute reads
    # as healthy while the archive says the symbol failed a third of the week. Measured
    # 2026-08-19: ADAUSD's live `nearest` was 0.648, comfortably inside a 0.700 floor, on a symbol
    # that had lost 38 % of its passes that week.
    archive_nearest_miss: Optional[float] = None
    # ...and the same distance averaged over those passes. Both are needed and they answer
    # different questions: the MINIMUM says "the floor came this close to saving a pass" (one
    # pass, the luckiest), the MEAN says "moving the floor by this much would recover most of
    # them". Reporting only the minimum overstates the case for a small adjustment, which is
    # exactly the mistake a calibration would then bake in.
    archive_miss_avg: Optional[float] = None

    @property
    def missed_by(self) -> Optional[float]:
        """How far past the floor the best candidate sat when the symbol went unscored."""
        if self.archive_nearest_miss is None:
            return None
        return self.archive_nearest_miss - self.floor

    @property
    def missed_by_avg(self) -> Optional[float]:
        """The same margin averaged over the unscored passes — the honest one to retune on."""
        if self.archive_miss_avg is None:
            return None
        return self.archive_miss_avg - self.floor

    @property
    def starved(self) -> bool:
        """Nothing in the window clears the floor — the prompt gets no context at all."""
        return self.nearest is not None and self.nearest > self.floor

    @property
    def indiscriminate(self) -> bool:
        """More than half the window is 'relevant' to one symbol — the cut is not cutting."""
        return self.median is not None and self.median <= self.floor

    @property
    def no_data_share(self) -> Optional[float]:
        return self.archive_no_data / self.archive_passes if self.archive_passes else None

    @property
    def foreign_share(self) -> Optional[float]:
        return self.foreign_in_floor / self.in_floor if self.in_floor else None


@dataclass
class FloorProfileReport:
    """Every query of one pipeline, worst-covered last — the floor seen from both sides."""
    pipeline_id: str
    config_file: str
    model: str
    floor: float
    window_minutes: int
    window_articles: int
    archive_label: str
    rows: List[FloorProfileRow] = field(default_factory=list)
    own_sources: List[str] = field(default_factory=list)   # this pipeline's set, for the header

    @property
    def starved_count(self) -> int:
        return sum(1 for row in self.rows if row.starved)

    @property
    def indiscriminate_count(self) -> int:
        return sum(1 for row in self.rows if row.indiscriminate)


def knee_of(distances: Sequence[float], within: Optional[float] = None) -> Optional[float]:
    """The largest gap in an ascending distance curve, reported as the value *below* the gap.

    ISSUE_55's deterministic baseline. Where the sorted distances jump, the corpus itself is
    separating "about this symbol" from "about something else" — so that jump is a floor
    candidate arrived at without an LLM and without a hand-set constant.

    `within` bounds the search, and it is not optional in practice. Measured on the dev corpus
    while building this: an unbounded search put `Litecoin LTC`'s knee at 0.885, *above* its own
    median — because the widest gap in a full curve is almost always out in the sparse tail,
    where a single distant article sits alone. That gap is real and says nothing about relevance.
    The caller passes the **median**, so the knee is looked for in the half of the curve that
    could plausibly be on-topic; the median is used rather than the floor on purpose, since a
    baseline derived from the floor could not be a cross-check on it.

    Returns the distance on the near side of the widest qualifying jump; None when there is
    nothing to separate (fewer than two samples in range).
    """
    ordered = sorted(d for d in distances if within is None or d <= within)
    if len(ordered) < 2:
        return None
    widest, at = 0.0, ordered[0]
    for lower, upper in zip(ordered, ordered[1:]):
        gap = upper - lower
        if gap > widest:
            widest, at = gap, lower
    return at


def build_floor_profile_report(
        database_url: str, symbol_queries: Dict[str, str], cache: QueryVectorCache, *,
        pipeline_id: str, config_file: str, model: str, window_minutes: int, floor: float,
        own_source_ids: Set[str], no_data_rows: Sequence[NoDataRow], archive_label: str = '7d',
        article_table: str = 'articles') -> FloorProfileReport:
    """Measure every query's distance profile against the live window.

    Args:
        symbol_queries: symbol -> query text, from the constellation.
        cache: the ISSUE_19 query-vector cache — a hit costs nothing.
        own_source_ids: the source ids of this pipeline's own set. The corpus is *shared*
            (`pgvector_store.query` applies no source-set filter), so anything outside this set
            reaching a prompt is a query pulling in another pipeline's feeds.
        no_data_rows: the archive half, from `build_no_data_report`. It only returns symbols that
            had a no-data pass, so an absent symbol means zero — which is what lets the decisive
            never-starved rows render at all.
    """
    # One row per distinct query text (ETHUSD + ETHEUR -> 'Ethereum ETH'), so a query is embedded
    # and measured once no matter how many symbols reuse it — the same grouping coverage_report
    # applies for the same reason.
    by_query: Dict[str, List[str]] = {}
    for symbol, text in symbol_queries.items():
        by_query.setdefault(text, []).append(symbol)

    archive = _archive_by_symbol(no_data_rows, pipeline_id)
    since_window = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    rows: List[FloorProfileRow] = []
    try:
        with psycopg.connect(database_url) as conn:
            register_vector(conn)                      # binds Python vectors to `%(v)s::vector`
            with conn.cursor() as cur:
                cur.execute(f'SELECT count(*) FROM {article_table} WHERE published_at >= %s',
                            (since_window,))
                window_articles = int(cur.fetchone()[0])
                for query_text, symbols in sorted(by_query.items()):
                    vector = cache.get_vector(query_text)     # cached -> no API call on a hit
                    rows.append(_measure(cur, query_text, sorted(symbols), vector,
                                         since_window=since_window, floor=floor,
                                         own_source_ids=own_source_ids,
                                         window_articles=window_articles,
                                         archive=archive, article_table=article_table))
    except psycopg.Error as exc:
        raise VectorStoreError(f'floor profile report failed: {exc}') from exc

    # Best-covered first, so the rows that need attention collect at the bottom next to the legend.
    rows.sort(key=lambda row: (row.nearest is None, row.nearest or 0.0))
    return FloorProfileReport(
        pipeline_id=pipeline_id, config_file=config_file, model=model, floor=floor,
        window_minutes=window_minutes, window_articles=window_articles,
        archive_label=archive_label, rows=rows, own_sources=sorted(own_source_ids))


def _measure(cur: psycopg.Cursor, query_text: str, symbols: List[str], vector: Any, *,
             since_window: datetime, floor: float, own_source_ids: Set[str],
             window_articles: int, archive: Dict[str, _Archive],
             article_table: str) -> FloorProfileRow:
    """One query: the distribution, the in-floor counts, and the knee."""
    # The distribution in one scan. `percentile_cont` is what separates this from coverage_report:
    # a mean cannot show that half a window sits inside the floor, and that is the whole question.
    cur.execute(
        f'SELECT min(embedding <=> %(v)s::vector), '
        'percentile_cont(0.1) WITHIN GROUP (ORDER BY embedding <=> %(v)s::vector), '
        'percentile_cont(0.5) WITHIN GROUP (ORDER BY embedding <=> %(v)s::vector), '
        'count(*) FILTER (WHERE (embedding <=> %(v)s::vector) <= %(floor)s), '
        'count(*) FILTER (WHERE (embedding <=> %(v)s::vector) <= %(floor)s '
        '                 AND NOT (source_id = ANY(%(own)s))) '
        f'FROM {article_table} WHERE published_at >= %(win)s',
        {'v': vector, 'win': since_window, 'floor': floor, 'own': list(own_source_ids)})
    nearest, p10, median, in_floor, foreign = cur.fetchone()
    # The knee needs the curve, not an aggregate — the nearest N distances, ascending.
    cur.execute(
        f'SELECT embedding <=> %(v)s::vector AS distance FROM {article_table} '
        'WHERE published_at >= %(win)s ORDER BY distance LIMIT %(cap)s',
        {'v': vector, 'win': since_window, 'cap': _KNEE_SAMPLE})
    curve = [float(row[0]) for row in cur.fetchall()]

    blank: _Archive = (0, 0, None, None)
    passes = sum(archive.get(symbol, blank)[0] for symbol in symbols)
    no_data = sum(archive.get(symbol, blank)[1] for symbol in symbols)
    # Several symbols can share a query; the closest miss among them is the one that matters —
    # it is the smallest margin by which the floor turned real news into a mechanical HOLD.
    misses = [archive[symbol][2] for symbol in symbols
              if symbol in archive and archive[symbol][2] is not None]
    mean_misses = [archive[symbol][3] for symbol in symbols
                   if symbol in archive and archive[symbol][3] is not None]
    return FloorProfileRow(
        query_text=query_text, symbols=symbols, window_articles=window_articles,
        nearest=_maybe(nearest), p10=_maybe(p10), median=_maybe(median),
        in_floor=int(in_floor), foreign_in_floor=int(foreign),
        knee=knee_of(curve, within=_maybe(median)), floor=floor,
        archive_passes=passes, archive_no_data=no_data,
        archive_nearest_miss=min(misses) if misses else None,
        archive_miss_avg=(sum(mean_misses) / len(mean_misses)) if mean_misses else None)


def _archive_by_symbol(no_data_rows: Sequence[NoDataRow],
                       pipeline_id: str) -> Dict[str, _Archive]:
    """symbol -> (passes, no_data, nearest miss, mean miss) for this pipeline, from the archive."""
    return {row.symbol: (row.passes, row.no_data_passes, row.nearest_miss_min,
                         row.nearest_miss_avg)
            for row in no_data_rows if row.pipeline_id == pipeline_id}


def _maybe(value: Optional[float]) -> Optional[float]:
    """Postgres returns NULL over an empty window — keep that as None rather than 0.0."""
    return None if value is None else float(value)


def _fmt(value: Optional[float]) -> str:
    return f'{value:.3f}' if value is not None else '  n/a'


def _pct(value: Optional[float]) -> str:
    return f'{value * 100:.1f} %' if value is not None else '   —'


def _margin(row: FloorProfileRow) -> str:
    """Closest and mean miss as signed distances from the floor: `+0.006/+0.031`.

    Both, because they support opposite conclusions from the same failure: a tiny minimum next to
    a large mean means one pass came close and the rest were nowhere near — so a small floor move
    would rescue almost nothing.
    """
    closest, mean = row.missed_by, row.missed_by_avg
    if closest is None:
        return '      —'
    return f'{closest:+.3f}/{mean:+.3f}' if mean is not None else f'{closest:+.3f}/    —'


def format_floor_profile_report(report: FloorProfileReport) -> str:
    """Render the profile as the shared console pattern (title + window line + dividers)."""
    divider = '-' * 104
    window_label = f'{report.window_minutes}min/{report.window_minutes / 60:.0f}h'
    lines = [
        f'Retrieval Floor Profile — {report.pipeline_id} · floor {report.floor:.3f} · '
        f'window {window_label} · {report.window_articles} articles',
        f'config: {report.config_file} · model {report.model} · '
        f'archive window {report.archive_label}',
        divider,
        f'{"query":22} {"nearest":>8} {"p10":>7} {"median":>7} {"<=floor":>8} {"foreign":>8} '
        f'{"knee":>7} {"mech.HOLD":>10} {"miss min/avg":>15}  symbols',
        divider,
    ]
    if not report.rows:
        lines.append('(no symbol queries configured for this pipeline)')
        return '\n'.join(lines + [divider])
    for row in report.rows:
        # Exception density, as everywhere else: a healthy row spends no marker.
        mark = ' ✗' if row.starved else (' ⚠' if row.indiscriminate else '  ')
        lines.append(
            f'{row.query_text:22.22} {_fmt(row.nearest):>8} {_fmt(row.p10):>7} '
            f'{_fmt(row.median):>7} {row.in_floor:>8} {row.foreign_in_floor:>8} '
            f'{_fmt(row.knee):>7}{mark} {_pct(row.no_data_share):>9} {_margin(row):>15}  '
            f'{", ".join(row.symbols)}')
    lines.append(divider)
    lines.append(
        f'{len(report.rows)} queries · {report.starved_count} starved · '
        f'{report.indiscriminate_count} indiscriminate · '
        f'own feeds: {", ".join(report.own_sources) if report.own_sources else "—"}')
    lines.append('')
    lines.extend([
        '✗  nearest article is ABOVE the floor — this query is scored on nothing in this window',
        '⚠  the window MEDIAN is inside the floor — more than half the corpus counts as relevant',
        '   to one symbol, so the cut is not discriminating for this query',
        'foreign   = in-floor articles from a feed outside this pipeline\'s own source set; the',
        '            corpus is shared, so a generic query pulls another pipeline\'s news',
        'knee      = largest gap in the nearest-200 curve BELOW the median — the deterministic',
        '            floor candidate the corpus itself suggests (ISSUE_55 cross-check)',
        'mech.HOLD = share of archive passes that produced a mechanical no_data HOLD',
        'miss      = how far PAST the floor the best candidate sat on the passes that produced',
        '            nothing: the CLOSEST such pass / the MEAN over them. The pair matters — a',
        '            small min beside a large mean means one pass nearly made it and the rest',
        '            were nowhere near, so a small floor move would rescue almost nothing.',
        '            It is also the only column that can see an EPISODIC failure: the live',
        '            columns describe this minute, and starvation comes and goes',
        '<=floor counts BEFORE dedup and top_k — fewer articles than this reach the prompt',
    ])
    return '\n'.join(lines)
