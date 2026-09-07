"""Integration tests for PgVectorStore — idempotency, recency, ordering, importance.

Skipped when psycopg/pgvector or a reachable PostgreSQL is missing, so the suite stays green
everywhere. Runs against the canonical `articles` table in the isolated, migration-built test
schema (`clean_db`, ISSUE_14) — i.e. at the real 1536 dimensions the corpus actually uses, not a
toy width. `_vec` keeps the vectors readable by padding: the leading components carry the
geometry, so the cosine relationships these tests assert on are unchanged.
"""
from datetime import datetime, timedelta, timezone
from typing import List

import psycopg
import pytest

from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.types.article_types import Article, NeighbourCount
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig

_DIMS = 1536
_TABLE = 'articles'
_BASE = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)


def _vec(*leading: float) -> List[float]:
    """A full-width embedding whose meaning lives in its first components."""
    return list(leading) + [0.0] * (_DIMS - len(leading))


def _article(article_id: str, published_at: datetime, source_id: str = 's') -> Article:
    return Article(
        article_id=article_id, source_id=source_id, source_weight=1.0,
        url=f'https://example.test/{article_id}', title=f'title-{article_id}',
        summary='summary', language='en', published_at=published_at, fetched_at=_BASE)


@pytest.fixture
def store(clean_db: str) -> PgVectorStore:
    return PgVectorStore(VectorStoreConfig(), clean_db, dimensions=_DIMS,
                         embedding_model='test-embed')


def test_upsert_is_idempotent(store):
    arts = [_article('a', _BASE), _article('b', _BASE)]
    vecs = [_vec(1.0), _vec(0.0, 1.0)]
    assert store.upsert(arts, vecs) == 2
    assert store.upsert(arts, vecs) == 0  # conflicts skipped → idempotent


def test_query_recency_and_similarity_order(store):
    old = _BASE - timedelta(days=10)
    store.upsert(
        [_article('near', _BASE), _article('far', _BASE), _article('old', old)],
        [_vec(1.0), _vec(0.0, 1.0), _vec(1.0)],
    )
    result = store.query(_vec(1.0), top_k=10, since=_BASE - timedelta(days=1))
    ids = [hit.article.article_id for hit in result]
    assert 'old' not in ids          # recency lower bound excludes the stale article
    assert ids[0] == 'near'          # identical vector → most similar first
    assert result[0].distance <= result[1].distance      # cosine distance, ascending
    assert result[0].embedding == _vec(1.0)              # stored embedding round-trips
    assert result[0].importance is None                  # tag populated later by #11


def test_query_min_importance_excludes_null(store):
    # upsert leaves importance NULL (populated later by #11) → filtered out when required
    store.upsert([_article('a', _BASE)], [_vec(1.0)])
    result = store.query(_vec(1.0), top_k=10,
                         since=_BASE - timedelta(days=1), min_importance=2)
    assert result == []


def test_count_neighbors_within_window_and_distance(store):
    # The breaking detector's cluster probe (ISSUE_11): near copies within the window count;
    # a dissimilar article and a stale one do not. Both measures come back (ISSUE_106) — here the
    # two neighbours share one feed, which is exactly the case the article count cannot see.
    old = _BASE - timedelta(days=10)
    store.upsert(
        [_article('n1', _BASE), _article('n2', _BASE),
         _article('far', _BASE), _article('old', old)],
        [_vec(1.0), _vec(1.0),
         _vec(0.0, 1.0), _vec(1.0)],
    )
    count = store.count_neighbors(_vec(1.0),
                                  since=_BASE - timedelta(days=1), max_distance=0.1)
    assert count.articles == 2   # n1 + n2 (distance 0); far excluded (distance 1), old (window)
    assert count.feeds == 1      # ...and both came from the same feed
    assert count.duplication == 2.0


def test_count_neighbors_counts_distinct_feeds_and_scopes_to_the_ones_asked_for(store):
    """The two halves of ISSUE_106's defect, in one fixture.

    Three identical vectors from three feeds is corroboration; the same three with one feed left
    out of `source_ids` is a smaller neighbourhood, because a set's threshold must be measured
    against that set's own feeds. Without the scope, `articles` being one table for every source
    set let a macro story carried elsewhere inflate this set's count.
    """
    store.upsert(
        [_article('a', _BASE, source_id='coindesk'),
         _article('b', _BASE, source_id='decrypt'),
         _article('c', _BASE, source_id='theblock')],
        [_vec(1.0), _vec(1.0), _vec(1.0)],
    )
    since = _BASE - timedelta(days=1)

    everything = store.count_neighbors(_vec(1.0), since=since, max_distance=0.1)
    assert (everything.articles, everything.feeds) == (3, 3)

    scoped = store.count_neighbors(_vec(1.0), since=since, max_distance=0.1,
                                   source_ids={'coindesk', 'decrypt'})
    assert (scoped.articles, scoped.feeds) == (2, 2), 'the third feed leaked past the scope'


def test_an_empty_neighbourhood_reports_no_duplication_rather_than_dividing_by_zero(store):
    empty = store.count_neighbors(_vec(1.0), since=_BASE - timedelta(days=1), max_distance=0.1)

    assert (empty.articles, empty.feeds) == (0, 0)
    assert empty.duplication is None


def test_flag_candidates_sets_tier_flag_and_timestamp(store, clean_db):
    # ISSUE_11: flagging stamps importance + breaking_candidate + flagged_at, idempotently.
    store.upsert([_article('a', _BASE)], [_vec(1.0)])
    assert store.flag_candidates(['a'], importance=3, breaking=True) == 1
    # importance now satisfies the deep-tier filter that a NULL failed above
    result = store.query(_vec(1.0), top_k=10,
                         since=_BASE - timedelta(days=1), min_importance=2)
    assert [hit.article.article_id for hit in result] == ['a']
    assert result[0].importance == 3
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT breaking_candidate, flagged_at FROM {_TABLE} '
                    'WHERE article_id = %s', ('a',))
        breaking, flagged_at = cur.fetchone()
    assert breaking is True and flagged_at is not None


def test_flag_candidates_nonexistent_id_is_noop(store):
    assert store.flag_candidates(['ghost'], importance=3, breaking=True) == 0


def test_a_cluster_flag_records_its_evidence_and_a_keyword_flag_does_not(store, clean_db):
    """ISSUE_106: the neighbourhood is written only where the cluster path produced the verdict.

    A keyword flag leaves both columns NULL rather than writing 0 — 0 would claim an empty
    neighbourhood was measured when none was consulted, which is the same distinction
    `detection_trigger` draws between a category and an absence.
    """
    store.upsert([_article('cluster', _BASE), _article('kw', _BASE)], [_vec(1.0), _vec(1.0)])

    store.flag_candidates(['cluster'], importance=2, breaking=False, trigger='cluster',
                          neighbours=NeighbourCount(articles=4, feeds=3))
    store.flag_candidates(['kw'], importance=3, breaking=True, trigger='keyword')

    with psycopg.connect(store._database_url) as conn, conn.cursor() as cur:
        cur.execute('SELECT article_id, detection_trigger, cluster_articles, cluster_feeds '
                    'FROM articles ORDER BY article_id')
        rows = {row[0]: row[1:] for row in cur.fetchall()}

    assert rows['cluster'] == ('cluster', 4, 3)
    assert rows['kw'] == ('keyword', None, None)
