"""BreakingDetector (ISSUE_11) — tier boundaries + keyword fast-path, all LLM-free.

Pure logic: a fake store with a controllable cluster size, so no DB and no API budget.
"""
from datetime import datetime, timezone
from typing import List, Set

from finiexragengine.core.pipeline.breaking_detector import HIGH, MID, BreakingDetector
from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.types.article_types import Article, ScoredArticle
from finiexragengine.types.config_types.source_set_types import DetectionConfig

_NOW = datetime.now(timezone.utc)


def _article(title: str, weight: float = 1.0, summary: str = '') -> Article:
    return Article(article_id=title, source_id='s', source_weight=weight,
                   url=f'https://x.test/{title}', title=title, summary=summary,
                   language='en', published_at=_NOW, fetched_at=_NOW)


class _FakeStore(AbstractVectorStore):
    """Reports a fixed cluster size for every neighbor query; records what got flagged."""

    def __init__(self, cluster_size: int) -> None:
        self._cluster_size = cluster_size
        self.flagged: List[tuple] = []            # (article_ids, importance, breaking)

    def existing_ids(self, article_ids: List[str]) -> Set[str]:
        return set()

    def upsert(self, articles, vectors) -> int:
        return len(articles)

    def query(self, *args, **kwargs) -> List[ScoredArticle]:
        return []

    def count_neighbors(self, vector, since, max_distance) -> int:
        return self._cluster_size

    def flag_candidates(self, article_ids, importance, breaking) -> int:
        self.flagged.append((list(article_ids), importance, breaking))
        return len(article_ids)


def _detect(cluster_size: int, article: Article, **cfg):
    store = _FakeStore(cluster_size)
    detector = BreakingDetector(store, DetectionConfig(**cfg))
    result = detector.detect([article], [[0.0, 0.0]])
    return store, result


def test_small_cluster_is_not_flagged():
    store, result = _detect(2, _article('a'))       # below mid_cluster_size (3)
    assert store.flagged == []
    assert result.max_tier == 0 and result.candidates == 0


def test_mid_cluster_flags_mid_not_candidate():
    store, result = _detect(3, _article('a'))       # == mid_cluster_size
    assert store.flagged == [(['a'], MID, False)]
    assert result.max_tier == MID and result.candidates == 0 and result.mid == 1


def test_high_cluster_flags_candidate():
    store, result = _detect(5, _article('a'))       # == high_cluster_size
    assert store.flagged == [(['a'], HIGH, True)]
    assert result.max_tier == HIGH and result.candidates == 1


def test_keyword_on_trusted_source_flags_high_without_a_cluster():
    # A single high-weight source + a breaking keyword -> HIGH immediately (fast-path).
    store, result = _detect(1, _article('Exchange hit by exploit', weight=1.0),
                            keywords=['exploit'], keyword_source_weight=0.9)
    assert store.flagged == [(['Exchange hit by exploit'], HIGH, True)]
    assert result.candidates == 1


def test_keyword_on_low_trust_source_does_not_fast_path():
    # Keyword present but the source is below keyword_source_weight -> no fast-path; small
    # cluster stays routine (unflagged).
    store, result = _detect(1, _article('rumor of an exploit', weight=0.5),
                            keywords=['exploit'], keyword_source_weight=0.9)
    assert store.flagged == []
    assert result.max_tier == 0


def test_keyword_is_word_boundary_not_substring():
    # "SEC" must not fire on "seconds"/"security".
    store, _ = _detect(1, _article('block confirmed in seconds', weight=1.0),
                       keywords=['SEC'], keyword_source_weight=0.9)
    assert store.flagged == []


def test_empty_batch_flags_nothing():
    store = _FakeStore(9)
    result = BreakingDetector(store, DetectionConfig()).detect([], [])
    assert store.flagged == [] and result.max_tier == 0
