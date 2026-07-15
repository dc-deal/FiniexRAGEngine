"""Tests for the corpus coverage diagnostic.

`test_format_*` is pure rendering (no DB). `test_build_*` seeds a tiny corpus into the isolated,
migration-built test schema (`clean_db`, ISSUE_14) and needs a reachable Postgres — skipped
otherwise, and it spends no API budget (the embedder is faked).
"""
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder  # noqa: E402
from finiexragengine.core.observability.reports.coverage_report import (  # noqa: E402
    CoverageReport,
    SymbolCoverage,
    build_coverage_report,
    format_coverage_report,
)
from finiexragengine.core.rag.pgvector_store import PgVectorStore  # noqa: E402
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache  # noqa: E402
from finiexragengine.types.article_types import Article  # noqa: E402
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig  # noqa: E402

_DIMS = 1536


def _vec(*leading: float) -> List[float]:
    """A full-width embedding whose meaning lives in its first components (see ISSUE_14:
    the corpus column is 1536 wide, so the tests use the real width)."""
    return list(leading) + [0.0] * (_DIMS - len(leading))


# --- pure formatting (no DB) --------------------------------------------------

def test_format_marks_generic_and_nan():
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    report = CoverageReport(
        pipeline_id='crypto_sentiment',
        config_file='configs/pipelines/crypto_sentiment.json',
        model='text-embedding-3-small', article_table='articles', total_articles=2,
        week_articles=2, window_articles=1, window_minutes=1440, floor=0.55,
        since_all=now - timedelta(days=6), since_week=now - timedelta(days=6),
        since_window=now - timedelta(hours=20), rows=[
            SymbolCoverage('Bitcoin BTC', ['BTCUSD'], 0.40, 0.70, 0.41, 0.71,
                           0.42, 0.72, 3, True),
            SymbolCoverage('Dash', ['DASHUSD'], 0.60, 0.70, float('nan'), float('nan'),
                           float('nan'), float('nan'), 0, False),
        ])
    text = format_coverage_report(report)
    assert 'Corpus Coverage Report' in text             # proper heading
    assert 'configs/pipelines/crypto_sentiment.json' in text   # config filename
    assert "pipeline 'crypto_sentiment'" in text        # provenance in the header
    assert 'text-embedding-3-small' in text
    assert '2 articles · 2 in 7d · 1 in the 1440min/24h window' in text
    assert 'ok' in text and 'GEN' in text               # covered vs generic fallback
    assert 'n/a' in text                                # NaN scope rendered as n/a
    assert 'week' in text                               # the new scope group
    assert 'from 07-04' in text                         # scope start stamps (all/week)
    assert 'from 07-09 16:00' in text                   # window's oldest article
    assert 'n≤f' in text                                # in-floor count column


# --- build against a seeded corpus (needs DB) ---------------------------------

class _FixedEmbedder(AbstractEmbedder):
    """Maps known query texts to hand-crafted unit vectors — no API, deterministic."""

    def embed(self, texts: List[str]) -> List[List[float]]:
        mapping = {'q_close': _vec(1.0), 'q_far': _vec(0.0, 0.0, 0.0, 1.0)}
        return [mapping[text] for text in texts]


@pytest.fixture
def seeded(clean_db: str):
    """A 3-article corpus (2 recent, 1 stale) + a query cache, in the empty test schema."""
    store = PgVectorStore(VectorStoreConfig(), clean_db, dimensions=_DIMS,
                          embedding_model='test-embed')
    cache = QueryVectorCache(_FixedEmbedder(), clean_db, model='m', dimensions=_DIMS)

    now = datetime.now(timezone.utc)

    def _article(article_id: str, published_at: datetime) -> Article:
        return Article(
            article_id=article_id, source_id='seed', source_weight=1.0,
            url=f'https://example.test/{article_id}', title=article_id, summary=article_id,
            language='en', published_at=published_at, fetched_at=now)

    # a1 sits exactly on q_close (distance 0); a2 is orthogonal; a3 == a1 but stale.
    articles = [_article('a1', now), _article('a2', now),
                _article('a3', now - timedelta(days=10))]
    store.upsert(articles, [_vec(1.0), _vec(0.0, 1.0), _vec(1.0)])
    return cache, clean_db


def test_build_covers_close_query_and_flags_far_one(seeded):
    cache, dsn = seeded
    report = build_coverage_report(
        {'SYM1': 'q_close', 'SYM2': 'q_far'}, cache, dsn,
        pipeline_id='test', config_file='configs/pipelines/test.json', model='m',
        window_minutes=1440)

    assert report.total_articles == 3
    assert report.week_articles == 2                    # a3 (10 days old) outside 7d too
    assert report.window_articles == 2                  # a3 is outside the 24h window
    # Scope starts = each scope's oldest article (the stats' real reach).
    assert report.since_all is not None and report.since_week is not None
    assert report.since_all < report.since_week         # a3 stretches all-time back
    by_query = {row.query_text: row for row in report.rows}

    close = by_query['q_close']
    assert close.covered is True
    assert close.best_distance == pytest.approx(0.0, abs=1e-6)   # a1 sits on the query
    assert close.window_in_floor == 1                   # only a1 is on-topic AND recent
    assert close.week_best_distance == pytest.approx(0.0, abs=1e-6)

    far = by_query['q_far']
    assert far.covered is False                         # orthogonal to every article
    assert far.best_distance > 0.55
    assert far.window_in_floor == 0                     # the mechanical no_data HOLD case

    assert report.rows[0].query_text == 'q_close'       # best-covered first
