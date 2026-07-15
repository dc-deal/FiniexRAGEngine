"""Live (paid) tests — real OpenAI embeddings through the full RAG path.

Fenced off by the `paid` marker: default runs and CI exclude them via pytest.ini
(`addopts = -m "not paid"`). Run deliberately:

    pytest -m paid -v

Needs OPENAI_API_KEY (environment / .env) and a reachable pgvector PostgreSQL.
Cost per run is far below one cent (a handful of short embedding inputs).
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip('openai')
pytest.importorskip('psycopg')

from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder  # noqa: E402
from finiexragengine.core.rag.pgvector_store import PgVectorStore  # noqa: E402
from finiexragengine.core.rag.query_vector_cache import QueryVectorCache  # noqa: E402
from finiexragengine.core.rag.retriever import Retriever  # noqa: E402
from finiexragengine.types.article_types import Article  # noqa: E402
from finiexragengine.types.config_types.app_config_types import (  # noqa: E402
    EmbeddingConfig,
    VectorStoreConfig,
)
from finiexragengine.types.config_types.pipeline_config_types import RetrievalConfig  # noqa: E402

pytestmark = [
    pytest.mark.paid,
    pytest.mark.skipif(not os.environ.get('OPENAI_API_KEY'),
                       reason='OPENAI_API_KEY not set'),
]



def _article(article_id: str, title: str, published_at: datetime,
             source_id: str = 'live-test') -> Article:
    return Article(
        article_id=article_id, source_id=source_id, source_weight=1.0,
        url=f'https://example.test/{article_id}', title=title, summary=title,
        language='en', published_at=published_at, fetched_at=published_at)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((sum(x * x for x in a) ** 0.5) * (sum(x * x for x in b) ** 0.5))


@pytest.fixture
def store(clean_db: str) -> PgVectorStore:
    # The isolated, migration-built test schema (ISSUE_14) — the operator's real corpus is
    # never touched, even by a paid run.
    return PgVectorStore(VectorStoreConfig(), clean_db, dimensions=1536,
                         embedding_model='text-embedding-3-small')


def test_live_embedding_dimension_and_semantics():
    embedder = OpenAIEmbedder(EmbeddingConfig())
    vectors = embedder.embed([
        'ECB signals another rate hike as euro area inflation stays elevated',
        'Euro strengthens after hawkish European Central Bank comments',
        'Bitcoin slides 5% after ETF outflows accelerate',
    ])
    assert [len(v) for v in vectors] == [1536, 1536, 1536]
    assert _cosine(vectors[0], vectors[1]) > _cosine(vectors[0], vectors[2])


def test_live_end_to_end_retrieval_squeeze(store, clean_db):
    embedder = OpenAIEmbedder(EmbeddingConfig())
    now = datetime.now(timezone.utc)
    articles = [
        _article('ecb', 'ECB signals another interest rate hike as euro area '
                        'inflation stays elevated', now - timedelta(hours=2)),
        _article('ecb-syndicated', 'ECB signals another interest rate hike as '
                                   'euro area inflation remains elevated',
                 now - timedelta(hours=1), source_id='other-feed'),
        _article('boe', 'Bank of England holds rates and warns on sterling weakness',
                 now - timedelta(hours=3)),
        _article('btc', 'Bitcoin slides after spot ETF outflows accelerate',
                 now - timedelta(hours=1)),
        _article('stale', 'Euro rallies on strong euro area PMI data',
                 now - timedelta(days=10)),
    ]
    vectors = embedder.embed([article.summary for article in articles])
    assert store.upsert(articles, vectors) == len(articles)

    config = RetrievalConfig(top_k=3, recency_window_minutes=1440, dedup_similarity=0.9)
    cache = QueryVectorCache(embedder, clean_db, model=EmbeddingConfig().model,
                             dimensions=1536)
    retriever = Retriever(cache, store, config)
    result = retriever.retrieve('Euro US Dollar EUR/USD euro area ECB')

    ids = [article.article_id for article in result]
    assert len(ids) <= 3
    assert 'stale' not in ids                          # outside the recency window
    assert ids[0] in ('ecb', 'ecb-syndicated')         # most similar story ranks first
    assert ('ecb' in ids) ^ ('ecb-syndicated' in ids)  # near-duplicate collapsed
