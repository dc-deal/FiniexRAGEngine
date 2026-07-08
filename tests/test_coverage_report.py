"""Tests for the corpus coverage diagnostic.

`test_format_*` is pure rendering (no DB). `test_build_*` seeds a tiny corpus and needs a
reachable pgvector Postgres — skipped otherwise, and it spends no API budget (the embedder
is faked). Point DATABASE_URL at a pgvector-enabled Postgres to run the integration test.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

pytest.importorskip('psycopg')
pytest.importorskip('pgvector')
import psycopg  # noqa: E402

from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder  # noqa: E402
from finiexragengine.core.rag.coverage_report import (  # noqa: E402
    CoverageReport,
    SymbolCoverage,
    build_coverage_report,
    format_coverage_report,
)
from finiexragengine.core.rag.pgvector_store import PgVectorStore  # noqa: E402
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache  # noqa: E402
from finiexragengine.exceptions.ragengine_errors import VectorStoreError  # noqa: E402
from finiexragengine.types.article_types import Article  # noqa: E402
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig  # noqa: E402

_DIMS = 4
_ART_TABLE = 'articles_coverage_test'
_QC_TABLE = 'query_vectors_coverage_test'


def _dsn() -> str:
    return os.environ.get(
        'DATABASE_URL', 'postgresql://ragengine:ragengine@127.0.0.1:5433/ragengine')


# --- pure formatting (no DB) --------------------------------------------------

def test_format_marks_generic_and_nan():
    report = CoverageReport(
        pipeline_id='crypto_sentiment', model='text-embedding-3-small',
        article_table='articles', total_articles=2, window_articles=1,
        window_minutes=1440, floor=0.55, rows=[
            SymbolCoverage('Bitcoin BTC', ['BTCUSD'], 0.40, 0.70, 0.42, 0.72, True),
            SymbolCoverage('Dash', ['DASHUSD'], 0.60, 0.70,
                           float('nan'), float('nan'), False),
        ])
    text = format_coverage_report(report)
    assert "pipeline 'crypto_sentiment'" in text        # provenance in the header
    assert 'text-embedding-3-small' in text
    assert '2 articles (1 within the 1440min/24h window)' in text
    assert 'ok' in text and 'GEN' in text               # covered vs generic fallback
    assert 'n/a' in text                                # NaN window rendered as n/a


# --- build against a seeded corpus (needs DB) ---------------------------------

class _FixedEmbedder(AbstractEmbedder):
    """Maps known query texts to hand-crafted unit vectors — no API, deterministic."""

    _MAP = {'q_close': [1.0, 0.0, 0.0, 0.0], 'q_far': [0.0, 0.0, 0.0, 1.0]}

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [self._MAP[text] for text in texts]


@pytest.fixture
def seeded():
    """A 3-article corpus (2 recent, 1 stale) + a query cache on clean test tables."""
    def _drop() -> None:
        with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {_ART_TABLE}')
            cur.execute(f'DROP TABLE IF EXISTS {_QC_TABLE}')

    try:
        _drop()
        store = PgVectorStore(VectorStoreConfig(table=_ART_TABLE), _dsn(), dimensions=_DIMS)
        cache = QueryVectorCache(_FixedEmbedder(), _dsn(), model='m',
                                 dimensions=_DIMS, table=_QC_TABLE)
    except (psycopg.Error, VectorStoreError) as exc:
        pytest.skip(f'PostgreSQL/pgvector not available: {exc}')

    now = datetime.now(timezone.utc)

    def _article(article_id: str, published_at: datetime) -> Article:
        return Article(
            article_id=article_id, source_id='seed', source_weight=1.0,
            url=f'https://example.test/{article_id}', title=article_id, summary=article_id,
            language='en', published_at=published_at, fetched_at=now)

    # a1 sits exactly on q_close (distance 0); a2 is orthogonal; a3 == a1 but stale.
    articles = [_article('a1', now), _article('a2', now),
                _article('a3', now - timedelta(days=10))]
    vectors = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]]
    store.upsert(articles, vectors)
    yield cache
    _drop()


def test_build_covers_close_query_and_flags_far_one(seeded):
    report = build_coverage_report(
        {'SYM1': 'q_close', 'SYM2': 'q_far'}, seeded, _dsn(),
        pipeline_id='test', model='m', window_minutes=1440, article_table=_ART_TABLE)

    assert report.total_articles == 3
    assert report.window_articles == 2                  # a3 is outside the 24h window
    by_query = {row.query_text: row for row in report.rows}

    close = by_query['q_close']
    assert close.covered is True
    assert close.best_distance == pytest.approx(0.0, abs=1e-6)   # a1 sits on the query

    far = by_query['q_far']
    assert far.covered is False                         # orthogonal to every article
    assert far.best_distance > 0.55

    assert report.rows[0].query_text == 'q_close'       # best-covered first
