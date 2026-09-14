"""Unit tests for Retriever — recency, top_k cap, dedup, deep tier, tie-breaks, funnel.

Embedder and store are faked, so these run offline. The fake store records the
query arguments (window, min_importance) and returns pre-built ScoredArticle
hits with controlled distances, weights and embeddings.
"""
import itertools
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.rag.retriever import Retriever
from finiexragengine.types.article_types import Article, ScoredArticle
from finiexragengine.types.config_types.pipeline_config_types import (
    DeepTierConfig,
    RetrievalConfig,
)

_TS = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
_DIMS = 64
_axis = itertools.count(1)   # axis 0 is the query vector


def _unit(index: int) -> List[float]:
    vector = [0.0] * _DIMS
    vector[index % _DIMS] = 1.0
    return vector


class _FakeQueryVectorCache:
    """Returns a fixed query vector (axis 0) and records the queries asked for."""

    def __init__(self) -> None:
        self.queries: List[str] = []

    def get_vector(self, query_text: str) -> List[float]:
        self.queries.append(query_text)
        return _unit(0)


class _FakeStore(AbstractVectorStore):
    """Pops one prepared response per query call and records the arguments."""

    def __init__(self, responses: List[List[ScoredArticle]]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []

    def upsert(self, articles: List[Article], vectors: List[List[float]]) -> int:
        raise AssertionError('retrieval must not upsert')

    def existing_ids(self, article_ids: List[str]) -> set:
        raise AssertionError('retrieval must not check existence')

    def query(self, vector, top_k, since, min_importance=None):
        self.calls.append({'top_k': top_k, 'since': since,
                           'min_importance': min_importance})
        return self._responses.pop(0) if self._responses else []


def _hit(article_id: str, distance: float, weight: float = 1.0,
         importance: Optional[int] = None,
         embedding: Optional[List[float]] = None) -> ScoredArticle:
    article = Article(
        article_id=article_id, source_id='s', source_weight=weight,
        url=f'https://example.test/{article_id}', title=f'title-{article_id}',
        summary='summary', language='en', published_at=_TS, fetched_at=_TS)
    return ScoredArticle(article=article, distance=distance,
                         embedding=embedding or _unit(next(_axis)),
                         importance=importance)


def _retriever(store: _FakeStore, **kwargs) -> Retriever:
    return Retriever(_FakeQueryVectorCache(), store, RetrievalConfig(**kwargs))


def test_recent_tier_window_and_overfetch():
    store = _FakeStore([[_hit('a', 0.1)]])
    retriever = _retriever(store, top_k=3, recency_window_minutes=60)
    before = datetime.now(timezone.utc)
    context = retriever.retrieve('query text')
    after = datetime.now(timezone.utc)
    assert [a.article_id for a in context.articles] == ['a']
    assert len(store.calls) == 1                      # deep tier off by default
    call = store.calls[0]
    assert call['top_k'] == 6                         # top_k * overfetch headroom
    assert call['min_importance'] is None
    assert before - timedelta(minutes=60) <= call['since'] <= after - timedelta(minutes=60)


def test_top_k_is_a_hard_cap():
    hits = [_hit(f'a{i}', 0.1 * i) for i in range(6)]
    store = _FakeStore([hits])
    context = _retriever(store, top_k=2).retrieve('q')
    assert [a.article_id for a in context.articles] == ['a0', 'a1']
    assert (context.funnel.in_window, context.funnel.kept) == (6, 2)


def test_orders_by_distance_within_tier():
    # floor off: this test checks pure distance ordering, not the relevance cut.
    store = _FakeStore([[_hit('far', 0.7), _hit('near', 0.1), _hit('mid', 0.4)]])
    context = _retriever(store, top_k=10, floor_distance=None).retrieve('q')
    assert [a.article_id for a in context.articles] == ['near', 'mid', 'far']


# --- relevance floor (ISSUE_24) ---

def test_floor_drops_off_topic_candidates():
    # 0.54 stays (on-topic), 0.56/0.70 exceed the default 0.55 floor -> dropped.
    store = _FakeStore([[_hit('on', 0.54), _hit('edge', 0.56), _hit('off', 0.70)]])
    context = _retriever(store, top_k=10).retrieve('q')
    assert [a.article_id for a in context.articles] == ['on']
    # The funnel records the cut: 3 offered, 2 dropped as off-topic, 1 reached the prompt —
    # and the spread + applied floor place the cut between best and worst candidate.
    funnel = context.funnel
    assert (funnel.in_window, funnel.floor_dropped, funnel.kept) == (3, 2, 1)
    assert (funnel.best_distance, funnel.worst_distance, funnel.floor) == (0.54, 0.70, 0.55)


def test_floor_can_empty_the_context():
    # Nothing on-topic: the empty result is the signal the evaluator's no_data
    # shortcut consumes — better an empty context than 12 generic articles.
    store = _FakeStore([[_hit('g1', 0.62), _hit('g2', 0.71)]])
    context = _retriever(store, top_k=10).retrieve('q')
    assert context.articles == []
    # The funnel explains the emptiness: window had candidates, the floor cut them —
    # and the nearest miss says how close the best one came (floor calibration signal).
    funnel = context.funnel
    assert (funnel.in_window, funnel.floor_dropped, funnel.kept) == (2, 2, 0)
    assert funnel.best_distance == 0.62


def test_floor_none_disables_the_cut():
    store = _FakeStore([[_hit('g1', 0.62), _hit('g2', 0.71)]])
    context = _retriever(store, top_k=10, floor_distance=None).retrieve('q')
    assert [a.article_id for a in context.articles] == ['g1', 'g2']
    assert context.funnel.floor_dropped == 0


def test_distance_tie_breaks_on_source_weight_then_importance():
    store = _FakeStore([[
        _hit('light', 0.2, weight=0.5, importance=3),
        _hit('heavy', 0.2, weight=1.0),
        _hit('untagged', 0.2, weight=0.5),
    ]])
    context = _retriever(store, top_k=10).retrieve('q')
    assert [a.article_id for a in context.articles] == ['heavy', 'light', 'untagged']


def test_near_duplicates_collapse_keeps_better_ranked():
    shared = _unit(1)
    related = [0.0] * _DIMS
    related[1] = 0.7071          # cosine ≈ 0.71 to `shared` — similar but no duplicate
    related[2] = 0.7071
    store = _FakeStore([[
        _hit('original', 0.1, embedding=list(shared)),
        _hit('syndicated', 0.2, embedding=list(shared)),
        _hit('related', 0.3, embedding=related),
    ]])
    context = _retriever(store, top_k=10).retrieve('q')
    assert [a.article_id for a in context.articles] == ['original', 'related']
    assert context.funnel.near_duplicates == 1


def test_deep_tier_opt_in_queries_and_ranks_behind_recent():
    recent = [_hit('recent', 0.5)]
    deep = [_hit('deep', 0.01, importance=3)]
    store = _FakeStore([recent, deep])
    retriever = _retriever(store, top_k=5, recency_window_minutes=60,
                           deep_tier=DeepTierConfig(min_importance=2, window_minutes=2880))
    context = retriever.retrieve('q')
    assert [a.article_id for a in context.articles] == ['recent', 'deep']   # recency dominates
    # ...and each survivor now says which window produced it (ISSUE_30).
    assert [(r.article.article_id, r.retrieval_tier) for r in context.retrieved] == [
        ('recent', 'recent'), ('deep', 'deep')]
    assert len(store.calls) == 2
    assert store.calls[1]['min_importance'] == 2
    assert store.calls[1]['since'] < store.calls[0]['since']      # deep window reaches back further


def test_recency_still_dominates_when_the_deep_hit_is_far_nearer():
    """The ranking pin, and it exists because ISSUE_30's own change nearly broke it.

    The tier used to be the integers 0/1 and `_rank_key` sorted on them directly. Turning it into
    the labels 'recent'/'deep' without a rank map would have sorted them ALPHABETICALLY — 'deep'
    before 'recent' — putting week-old articles ahead of today's news at the top of the prompt.
    Silent, and the precise contamination this issue exists to prevent.

    The distances here make it unmissable: the deep hit is at 0.01 against the recent tier's 0.90,
    so any ordering that is not tier-first puts it first.
    """
    store = _FakeStore([[_hit('today', 0.90)], [_hit('last-week', 0.01, importance=3)]])
    retriever = _retriever(store, top_k=5, floor_distance=None,
                           deep_tier=DeepTierConfig(min_importance=2))
    context = retriever.retrieve('q')

    assert [r.retrieval_tier for r in context.retrieved] == ['recent', 'deep']
    assert [a.article_id for a in context.articles] == ['today', 'last-week']


def test_a_recent_only_retrieval_marks_every_survivor_recent():
    """Never `None` and never blank: the tier is set at the seam, so the envelope's `None`
    keeps its single meaning of "archived before the field existed"."""
    store = _FakeStore([[_hit('a', 0.1), _hit('b', 0.2)]])
    context = _retriever(store, top_k=5).retrieve('q')

    assert [r.retrieval_tier for r in context.retrieved] == ['recent', 'recent']
    assert context.funnel.deep_kept == 0


def test_deep_kept_is_the_count_of_deep_survivors_not_a_parallel_tally():
    """One computation, so the funnel's number and the envelope's per-citation tiers agree.

    They used to be two — a counter accumulated beside the list — and two numbers meant to agree
    is the shape ISSUE_82 spent weeks on. The day they diverge is the day the funnel lies.
    """
    store = _FakeStore([
        [_hit('r1', 0.10), _hit('r2', 0.20)],
        [_hit('d1', 0.30, importance=3), _hit('d2', 0.40, importance=3)],
    ])
    context = _retriever(store, top_k=10, floor_distance=None,
                         deep_tier=DeepTierConfig(min_importance=2)).retrieve('q')

    deep_in_list = sum(1 for r in context.retrieved if r.retrieval_tier == 'deep')
    assert context.funnel.deep_kept == deep_in_list == 2
    assert context.funnel.kept == len(context.retrieved) == 4


def test_deep_tier_does_not_duplicate_recent_articles():
    store = _FakeStore([
        [_hit('both-tiers', 0.2)],
        [_hit('both-tiers', 0.2, importance=3)],
    ])
    retriever = _retriever(store, top_k=5, deep_tier=DeepTierConfig())
    context = retriever.retrieve('q')
    assert [a.article_id for a in context.articles] == ['both-tiers']
    # Both tiers offered it, one copy was collapsed — visible in the funnel.
    assert (context.funnel.in_window, context.funnel.tier_duplicates) == (2, 1)


def test_empty_store_yields_empty_context():
    store = _FakeStore([[]])
    context = _retriever(store, top_k=5).retrieve('q')
    assert context.articles == []
    # An empty *window* (vs a floor cut) is distinguishable: no candidates, no spread.
    funnel = context.funnel
    assert (funnel.in_window, funnel.floor_dropped, funnel.kept) == (0, 0, 0)
    assert funnel.best_distance is None and funnel.worst_distance is None
    assert funnel.floor == 0.55                       # the cut that *would* have applied
