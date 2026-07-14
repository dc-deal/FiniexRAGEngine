"""Tests for the Ingestor — per-source new/dup, no re-embedding of known ids.

Pure logic: fake source/store/embedder, so no DB and no API budget are touched.
"""
from datetime import datetime, timezone
from typing import List, Optional

from finiexragengine.core.pipeline.ingestor import Ingestor
from finiexragengine.types.ingest_types import HealthOutcome
from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.exceptions.ragengine_errors import BudgetExceededError, SourceFetchError
from finiexragengine.types.article_types import Article, ScoredArticle
from finiexragengine.types.config_types.source_set_types import SourceConfig

_NOW = datetime.now(timezone.utc)


def _article(article_id: str) -> Article:
    return Article(article_id=article_id, source_id='fake', source_weight=1.0,
                   url=f'https://example.test/{article_id}', title=article_id,
                   summary=article_id, language='en',
                   published_at=_NOW, fetched_at=_NOW)


class _FakeSource(AbstractSource):
    """Returns a fixed article list, or raises like an unreachable feed."""

    def __init__(self, source_id: str, articles: Optional[List[Article]] = None,
                 fail: bool = False, due: bool = True) -> None:
        super().__init__(SourceConfig(source_id=source_id, url='https://example.test'))
        self._articles = articles or []
        self._fail = fail
        self._due = due

    def due_for_fetch(self) -> bool:
        return self._due

    def fetch(self) -> List[Article]:
        if self._fail:
            raise SourceFetchError(f'{self.get_source_id()}: unreachable')
        return self._articles


class _CountingEmbedder(AbstractEmbedder):
    """Deterministic vectors; records how many texts were embedded (the spend)."""

    def __init__(self) -> None:
        self.total = 0

    def embed(self, texts: List[str]) -> List[List[float]]:
        self.total += len(texts)
        return [[float(len(text)), 0.0, 0.0, 0.0] for text in texts]


class _FakeStore(AbstractVectorStore):
    """In-memory idempotent store — knows which ids it already holds."""

    def __init__(self) -> None:
        self.seen = set()

    def existing_ids(self, article_ids: List[str]) -> set:
        return {article_id for article_id in article_ids if article_id in self.seen}

    def upsert(self, articles: List[Article], vectors: List[List[float]]) -> int:
        new = 0
        for article in articles:
            if article.article_id not in self.seen:
                self.seen.add(article.article_id)
                new += 1
        return new

    def query(self, vector, top_k, since, min_importance=None) -> List[ScoredArticle]:
        return []


def test_fetches_embeds_and_stores():
    source = _FakeSource('s1', [_article('a1'), _article('a2')])
    embedder = _CountingEmbedder()
    result = Ingestor([source], embedder, _FakeStore()).run()
    assert (result.fetched, result.embedded, result.stored, result.duplicates) == (2, 2, 2, 0)
    entry = result.per_source['s1']
    assert (entry.fetched, entry.embedded, entry.stored, entry.duplicates) == (2, 2, 2, 0)
    assert embedder.total == 2


def test_rerun_skips_known_ids_no_reembed():
    source = _FakeSource('s1', [_article('a1'), _article('a2')])
    embedder = _CountingEmbedder()
    ingestor = Ingestor([source], embedder, _FakeStore())
    ingestor.run()
    assert embedder.total == 2
    second = ingestor.run()
    assert second.fetched == 2                 # the feed still surfaces them
    assert second.embedded == 0                # but nothing known is re-embedded (no spend)
    assert second.stored == 0
    assert second.duplicates == 2
    assert second.per_source['s1'].duplicates == 2
    assert embedder.total == 2                 # unchanged — the second pass paid nothing


def test_failing_source_is_recorded_others_proceed():
    good = _FakeSource('good', [_article('a1')])
    bad = _FakeSource('bad', fail=True)
    result = Ingestor([bad, good], _CountingEmbedder(), _FakeStore()).run()
    assert result.stored == 1                  # the good source still ingested
    assert 'bad' in result.failed_sources
    assert 'bad' not in result.per_source
    assert result.per_source['good'].stored == 1


class _FakeHealth:
    """In-memory stand-in for SourceHealthStore (ISSUE_11) — no DB."""

    def __init__(self, quarantined=()):
        self.quarantined = set(quarantined)
        self.successes = []
        self.failures = []

    def should_poll(self, source_id):
        return source_id not in self.quarantined

    def record_success(self, source_id, host, source_set):
        self.successes.append((source_id, host))
        return False

    def record_failure(self, source_id, host, source_set, *, error_type, status, message):
        self.failures.append((source_id, error_type))
        return HealthOutcome(consecutive_failures=1, just_flagged=False, quarantined_until=None)


def test_health_records_success_and_typed_failure():
    good = _FakeSource('good', [_article('a1')])
    bad = _FakeSource('bad', fail=True)
    health = _FakeHealth()
    result = Ingestor([bad, good], _CountingEmbedder(), _FakeStore(),
                      health_store=health, source_set_id='crypto_news').run()
    assert ('good', 'example.test') in health.successes    # reachable poll -> success + host
    assert ('bad', 'UNREACHABLE') in health.failures       # typed failure recorded
    assert 'bad' in result.health_notes                    # carried for the worker's log level


def test_quarantined_source_is_skipped_not_polled():
    good = _FakeSource('good', [_article('a1')])
    bad = _FakeSource('bad', fail=True)
    health = _FakeHealth(quarantined={'bad'})
    result = Ingestor([bad, good], _CountingEmbedder(), _FakeStore(),
                      health_store=health, source_set_id='crypto_news').run()
    assert result.quarantined_skips == ['bad']             # skipped entirely (no poll)
    assert 'bad' not in result.failed_sources              # not polled -> not a failure
    assert health.failures == []                           # never hit while quarantined
    assert result.stored == 1                              # the good source still ingested


class _SuspendedEmbedder(AbstractEmbedder):
    """Stands in for the circuit-breaker gate (ISSUE_47): embedding is suspended (provider quota)."""

    def embed(self, texts):
        raise BudgetExceededError('embedding suspended — provider quota reached')


def test_budget_suspend_skips_embedding_no_crash():
    # A quota suspend degrades the ingest pass cleanly: stored 0, suspended flag set — not a crash.
    source = _FakeSource('s1', [_article('a1')])
    result = Ingestor([source], _SuspendedEmbedder(), _FakeStore()).run()
    assert result.suspended is True
    assert result.stored == 0                        # nothing embedded/stored while suspended
    assert result.fetched == 1                       # fetch still happened (free)


def test_floor_skipped_source_records_no_health():
    # A source within its poll floor is a local no-op — not a poll, so no success/failure is
    # recorded (otherwise a floor skip would reset a failing feed's streak and hide it).
    slow = _FakeSource('slow', [_article('a1')], due=False)
    fast = _FakeSource('fast', [_article('a2')])
    health = _FakeHealth()
    result = Ingestor([slow, fast], _CountingEmbedder(), _FakeStore(),
                      health_store=health, source_set_id='crypto_news').run()
    assert result.floor_skips == ['slow']                  # skipped as a no-op
    assert [s for s, _ in health.successes] == ['fast']    # only the polled source recorded
    assert 'slow' not in result.per_source
