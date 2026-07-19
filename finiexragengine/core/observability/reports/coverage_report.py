"""Corpus coverage diagnostic — how well the stored corpus covers each symbol query.

Read-only report over the shared pgvector corpus (ISSUE_3). For every symbol query it
measures the nearest article distance (best coverage) and the mean distance across three
scopes — all-time, last week, and the constellation's retrieval window (what a live
retrieval actually sees) — plus how many window articles survive the relevance floor
(ISSUE_24): that count is what would actually reach the prompt, and 0 predicts the
mechanical no_data HOLD. Each scope also reports the oldest article it contains, so the
stats are readable in context (an 'all-time' over a week-old corpus is just a week).
It is the tuning instrument for `retrieval.floor_distance`.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg
from pgvector.psycopg import register_vector

from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# Best-distance beyond which a symbol is only matched by generic, off-topic articles.
# ~0.55 empirically separates 'has its own news' from 'generic altcoin fallback' on the
# crypto corpus (see docs/development/database_inspection.md). The live pipelines carry
# their own value in `retrieval.floor_distance` (ISSUE_24); this is the fallback.
COVERAGE_FLOOR = 0.55

_WEEK_MINUTES = 7 * 24 * 60


def _as_float(value: Any) -> float:
    """DB numeric -> float; NULL (empty result set) -> NaN so callers can flag it."""
    return float(value) if value is not None else float('nan')


def _fmt(value: float) -> str:
    """Render a distance, or 'n/a' for the NaN produced by an empty corpus/window."""
    return f'{value:.3f}' if value == value else '  n/a'   # value != value tests for NaN


def _fmt_since(value: Optional[datetime]) -> str:
    """Oldest-article stamp of a scope: 'from MM-DD HH:MM' (UTC), or n/a when empty."""
    if value is None:
        return 'n/a'
    return value.astimezone(timezone.utc).strftime('from %m-%d %H:%M')


@dataclass
class SymbolCoverage:
    """One query text's coverage — all-time, last week, and the retrieval window."""
    query_text: str
    symbols: List[str]              # symbols sharing this query text (e.g. ETHUSD, ETHEUR)
    best_distance: float            # all-time nearest — lower is better coverage
    mean_distance: float            # all-time mean over the whole corpus
    week_best_distance: float       # nearest within the last 7 days (trend vs all-time)
    week_mean_distance: float
    window_best_distance: float     # nearest within the recency window (live retrieval view)
    window_mean_distance: float
    window_in_floor: int            # window articles within the floor = what reaches the prompt
    covered: bool                   # all-time best_distance <= floor (has own news at all)


@dataclass
class CoverageReport:
    """A full coverage run: provenance + corpus size + scope starts + per-query rows."""
    pipeline_id: str                # which constellation was analysed
    config_file: str                # config path shown in the report header
    model: str                      # embedding model of the cached query vectors
    article_table: str              # corpus table the report ran against
    total_articles: int
    week_articles: int              # articles inside the last 7 days
    window_articles: int            # articles inside the recency window
    window_minutes: int
    floor: float
    since_all: Optional[datetime]     # oldest article per scope — the stats' real reach
    since_week: Optional[datetime]
    since_window: Optional[datetime]
    rows: List[SymbolCoverage]


def build_coverage_report(
        symbol_queries: Dict[str, str], cache: QueryVectorCache, database_url: str, *,
        pipeline_id: str, config_file: str, model: str, window_minutes: int,
        article_table: str = 'articles', floor: float = COVERAGE_FLOOR) -> CoverageReport:
    """Per-symbol corpus coverage over three scopes, best-covered first.

    Ensures every symbol query is embedded and cached (idempotent — a cache hit spends
    nothing, ISSUE_19), then aggregates nearest/mean cosine distance per query for
    all-time / last week / `window_minutes`, plus the count of window articles inside
    `floor` (the post-ISSUE_24 prompt context; 0 = the mechanical no_data HOLD case).

    Args:
        symbol_queries: symbol -> retrieval query text (the constellation's map).
        cache: query-vector cache; resolves each query text to its (cached) vector.
        database_url: DSN of the pgvector Postgres holding the corpus.
        pipeline_id: constellation id, for provenance in the report header.
        model: embedding model name, for provenance.
        window_minutes: recency window (the pipeline's retrieval.recency_window_minutes).
        article_table: corpus table name (migrations-owned; default matches pgvector_store).
        floor: relevance floor distance (the pipeline's retrieval.floor_distance).

    Returns:
        A CoverageReport with one SymbolCoverage per distinct query text (ascending best).
    """
    # Group symbols by their shared query text (ETHUSD + ETHEUR -> 'Ethereum ETH'), so a
    # query is embedded and measured once even when several symbols reuse it.
    by_query: Dict[str, List[str]] = {}
    for symbol, text in symbol_queries.items():
        by_query.setdefault(text, []).append(symbol)

    now = datetime.now(timezone.utc)
    since_window = now - timedelta(minutes=window_minutes)
    since_week = now - timedelta(minutes=_WEEK_MINUTES)
    rows: List[SymbolCoverage] = []
    try:
        with psycopg.connect(database_url) as conn:
            register_vector(conn)                      # bind Python vectors to `%(v)s::vector`
            with conn.cursor() as cur:
                # Corpus size + the oldest article per scope: 'all-time' is only as old
                # as the corpus actually is — the stamp makes the stats honest.
                cur.execute(
                    f'SELECT count(*), '
                    f'count(*) FILTER (WHERE published_at >= %(week)s), '
                    f'count(*) FILTER (WHERE published_at >= %(win)s), '
                    f'min(published_at), '
                    f'min(published_at) FILTER (WHERE published_at >= %(week)s), '
                    f'min(published_at) FILTER (WHERE published_at >= %(win)s) '
                    f'FROM {article_table}',
                    {'week': since_week, 'win': since_window})
                (total_articles, week_articles, window_articles,
                 oldest_all, oldest_week, oldest_window) = cur.fetchone()

                for query_text, symbols in by_query.items():
                    vector = cache.get_vector(query_text)   # cached — no API call on a hit (#19)
                    # Nearest/mean distance per scope + the in-floor window count, one scan.
                    cur.execute(
                        f'SELECT min(embedding <=> %(v)s::vector), '
                        f'avg(embedding <=> %(v)s::vector), '
                        f'min(embedding <=> %(v)s::vector) FILTER (WHERE published_at >= %(week)s), '
                        f'avg(embedding <=> %(v)s::vector) FILTER (WHERE published_at >= %(week)s), '
                        f'min(embedding <=> %(v)s::vector) FILTER (WHERE published_at >= %(win)s), '
                        f'avg(embedding <=> %(v)s::vector) FILTER (WHERE published_at >= %(win)s), '
                        f'count(*) FILTER (WHERE published_at >= %(win)s '
                        f'AND (embedding <=> %(v)s::vector) <= %(floor)s) '
                        f'FROM {article_table}',
                        {'v': vector, 'week': since_week, 'win': since_window, 'floor': floor})
                    (best, mean, wk_best, wk_mean,
                     win_best, win_mean, in_floor) = cur.fetchone()
                    rows.append(SymbolCoverage(
                        query_text=query_text, symbols=sorted(symbols),
                        best_distance=_as_float(best), mean_distance=_as_float(mean),
                        week_best_distance=_as_float(wk_best),
                        week_mean_distance=_as_float(wk_mean),
                        window_best_distance=_as_float(win_best),
                        window_mean_distance=_as_float(win_mean),
                        window_in_floor=int(in_floor),
                        covered=best is not None and _as_float(best) <= floor))
    except psycopg.Error as exc:
        raise VectorStoreError(f'coverage report failed: {exc}') from exc
    rows.sort(key=lambda r: r.best_distance)
    return CoverageReport(
        pipeline_id=pipeline_id, config_file=config_file, model=model,
        article_table=article_table, total_articles=total_articles,
        week_articles=week_articles, window_articles=window_articles,
        window_minutes=window_minutes, floor=floor,
        since_all=oldest_all, since_week=oldest_week, since_window=oldest_window,
        rows=rows)


def format_coverage_report(report: CoverageReport) -> str:
    """Render a CoverageReport as the console pattern table (best-covered first)."""
    win_label = f'{report.window_minutes}min/{report.window_minutes / 60:.0f}h'
    divider = '-' * 92
    # Three scope groups, each 16 wide (best 7 + mean 8): the 'from' row beneath the
    # group titles shows each scope's oldest article — where its counting really starts.
    lines = [
        'Corpus Coverage Report',
        f"config: {report.config_file}  (pipeline '{report.pipeline_id}')",
        f'model {report.model} | table {report.article_table} | floor {report.floor:.2f}',
        f'corpus: {report.total_articles} articles · {report.week_articles} in 7d · '
        f'{report.window_articles} in the {win_label} window',
        divider,
        f'{"all-time":>15} {"week":>16} {"window":>16}',
        f'{_fmt_since(report.since_all):>15} {_fmt_since(report.since_week):>16} '
        f'{_fmt_since(report.since_window):>16}',
        f'{"best":>7} {"mean":>7} {"best":>8} {"mean":>7} {"best":>8} {"mean":>7} '
        f'{"n≤f":>5}  cov  symbols / query',
        divider,
    ]
    for r in report.rows:
        mark = ' ok' if r.covered else 'GEN'   # GEN = generic fallback (no own news, ever)
        lines.append(
            f'{_fmt(r.best_distance):>7} {_fmt(r.mean_distance):>7} '
            f'{_fmt(r.week_best_distance):>8} {_fmt(r.week_mean_distance):>7} '
            f'{_fmt(r.window_best_distance):>8} {_fmt(r.window_mean_distance):>7} '
            f'{r.window_in_floor:>5}  {mark}  '
            f'{", ".join(r.symbols)}  "{r.query_text}"')
    lines.append('')
    lines.append(
        f'floor {report.floor:.2f} distance — "GEN" = nearest all-time match beyond the floor '
        '(no own news, ever); n≤f = window articles within the floor = the live prompt '
        'context after ISSUE_24 (0 → mechanical no_data HOLD, no LLM call).')
    return '\n'.join(lines)
