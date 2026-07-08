"""Corpus coverage diagnostic — how well the stored corpus covers each symbol query.

Read-only report over the shared pgvector corpus (ISSUE_3). For every symbol query it
measures the nearest article distance (best coverage) and the mean distance — both
all-time and within the constellation's retrieval window (what a live retrieval would
actually see). A large best-distance means no article sits close to that symbol, so
retrieval falls back to generic, off-topic context — the 'No relevant news' case of the
output contract. It is the empirical companion to the retrieval floor question: which
symbols have their own news, and which pull only generic altcoin coverage.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import psycopg
from pgvector.psycopg import register_vector

from finiexragengine.core.rag.query_vector_cache import QueryVectorCache
from finiexragengine.exceptions.ragengine_errors import VectorStoreError

# Best-distance beyond which a symbol is only matched by generic, off-topic articles.
# ~0.55 empirically separates 'has its own news' from 'generic altcoin fallback' on the
# crypto corpus (see docs/development/database_inspection.md).
COVERAGE_FLOOR = 0.55


def _as_float(value) -> float:
    """DB numeric -> float; NULL (empty result set) -> NaN so callers can flag it."""
    return float(value) if value is not None else float('nan')


def _fmt(value: float) -> str:
    """Render a distance, or 'n/a' for the NaN produced by an empty corpus/window."""
    return f'{value:.3f}' if value == value else '  n/a'   # value != value tests for NaN


@dataclass
class SymbolCoverage:
    """One query text's coverage — all-time and within the retrieval window."""
    query_text: str
    symbols: List[str]              # symbols sharing this query text (e.g. ETHUSD, ETHEUR)
    best_distance: float            # all-time nearest — lower is better coverage
    mean_distance: float            # all-time mean over the whole corpus
    window_best_distance: float     # nearest within the recency window (live retrieval view)
    window_mean_distance: float     # mean within the recency window
    covered: bool                   # all-time best_distance <= floor


@dataclass
class CoverageReport:
    """A full coverage run: provenance + corpus size + per-query rows."""
    pipeline_id: str                # which constellation was analysed
    config_file: str                # config path shown in the report header
    model: str                      # embedding model of the cached query vectors
    article_table: str              # corpus table the report ran against
    total_articles: int
    window_articles: int            # articles inside the recency window
    window_minutes: int
    floor: float
    rows: List[SymbolCoverage]


def build_coverage_report(
        symbol_queries: Dict[str, str], cache: QueryVectorCache, database_url: str, *,
        pipeline_id: str, config_file: str, model: str, window_minutes: int,
        article_table: str = 'articles', floor: float = COVERAGE_FLOOR) -> CoverageReport:
    """Per-symbol corpus coverage, best-covered first.

    Ensures every symbol query is embedded and cached (idempotent — a cache hit spends
    nothing, ISSUE_19), then aggregates nearest/mean cosine distance per query, both
    all-time and within `window_minutes` (the recency window retrieval actually uses).

    Args:
        symbol_queries: symbol -> retrieval query text (the constellation's map).
        cache: query-vector cache; resolves each query text to its (cached) vector.
        database_url: DSN of the pgvector Postgres holding the corpus.
        pipeline_id: constellation id, for provenance in the report header.
        model: embedding model name, for provenance.
        window_minutes: recency window (the pipeline's retrieval.recency_window_minutes).
        article_table: corpus table name (app_config vector_store.table).
        floor: best-distance cut-off that marks a symbol as 'generic fallback'.

    Returns:
        A CoverageReport with one SymbolCoverage per distinct query text (ascending best).
    """
    # Group symbols by their shared query text (ETHUSD + ETHEUR -> 'Ethereum ETH'), so a
    # query is embedded and measured once even when several symbols reuse it.
    by_query: Dict[str, List[str]] = {}
    for symbol, text in symbol_queries.items():
        by_query.setdefault(text, []).append(symbol)

    since = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    rows: List[SymbolCoverage] = []
    try:
        with psycopg.connect(database_url) as conn:
            register_vector(conn)                      # bind Python vectors to `%(v)s::vector`
            with conn.cursor() as cur:
                # Corpus size: total + how many fall inside the recency window.
                cur.execute(
                    f'SELECT count(*), count(*) FILTER (WHERE published_at >= %(since)s) '
                    f'FROM {article_table}', {'since': since})
                total_articles, window_articles = cur.fetchone()

                for query_text, symbols in by_query.items():
                    vector = cache.get_vector(query_text)   # cached — no API call on a hit (#19)
                    # Nearest/mean distance all-time and window-filtered, in one scan.
                    cur.execute(
                        f'SELECT min(embedding <=> %(v)s::vector), '
                        f'avg(embedding <=> %(v)s::vector), '
                        f'min(embedding <=> %(v)s::vector) FILTER (WHERE published_at >= %(since)s), '
                        f'avg(embedding <=> %(v)s::vector) FILTER (WHERE published_at >= %(since)s) '
                        f'FROM {article_table}', {'v': vector, 'since': since})
                    best, mean, w_best, w_mean = cur.fetchone()
                    rows.append(SymbolCoverage(
                        query_text=query_text, symbols=sorted(symbols),
                        best_distance=_as_float(best), mean_distance=_as_float(mean),
                        window_best_distance=_as_float(w_best),
                        window_mean_distance=_as_float(w_mean),
                        covered=best is not None and _as_float(best) <= floor))
    except psycopg.Error as exc:
        raise VectorStoreError(f'coverage report failed: {exc}') from exc
    rows.sort(key=lambda r: r.best_distance)
    return CoverageReport(
        pipeline_id=pipeline_id, config_file=config_file, model=model,
        article_table=article_table, total_articles=total_articles,
        window_articles=window_articles, window_minutes=window_minutes,
        floor=floor, rows=rows)


def format_coverage_report(report: CoverageReport) -> str:
    """Render a CoverageReport as a compact console table (best-covered first)."""
    win_label = f'{report.window_minutes}min/{report.window_minutes / 60:.0f}h'
    divider = '-' * 74
    lines = [
        'Corpus Coverage Report',
        f"config: {report.config_file}  (pipeline '{report.pipeline_id}')",
        f'model {report.model} | table {report.article_table}',
        f'corpus: {report.total_articles} articles '
        f'({report.window_articles} within the {win_label} window)',
        divider,
        f'{"all-time":>13}   {"window":>13}',
        f'{"best":>6} {"mean":>6}   {"best":>6} {"mean":>6}  cov  symbols / query',
        divider,
    ]
    for r in report.rows:
        mark = ' ok' if r.covered else 'GEN'   # GEN = generic fallback (no own news)
        lines.append(
            f'{_fmt(r.best_distance):>6} {_fmt(r.mean_distance):>6}   '
            f'{_fmt(r.window_best_distance):>6} {_fmt(r.window_mean_distance):>6}  {mark}  '
            f'{", ".join(r.symbols)}  "{r.query_text}"')
    lines.append('')
    lines.append(
        f'floor {report.floor:.2f} distance — "GEN" = nearest all-time match beyond the floor '
        '(only generic/off-topic context, the HOLD case); window = what live retrieval sees now.')
    return '\n'.join(lines)
