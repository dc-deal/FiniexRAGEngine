"""Tests for the Ingestor — per-source new/dup, no re-embedding of known ids.

Pure logic: fake source/store/embedder, so no DB and no API budget are touched.
"""
from datetime import datetime, timezone
from typing import List, Optional

from finiexragengine.core.pipeline.ingestor import Ingestor
from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.exceptions.ragengine_errors import BudgetExceededError, SourceFetchError
from finiexragengine.types.article_types import Article, ScoredArticle
from finiexragengine.types.config_types.source_set_types import SourceConfig
from finiexragengine.types.embedding_types import EmbedResult
from finiexragengine.types.ingest_types import HealthOutcome

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

    def embed(self, texts: List[str]) -> EmbedResult:
        self.total += len(texts)
        return EmbedResult(
            vectors=[[float(len(text)), 0.0, 0.0, 0.0] for text in texts],
            input_tokens=[len(text.split()) for text in texts],
            truncated_tokens=[None] * len(texts))


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

    def __init__(self, quarantined=(), until=None):
        self.quarantined = set(quarantined)
        self._until = until
        self.successes = []
        self.failures = []

    def should_poll(self, source_id):
        return source_id not in self.quarantined

    def quarantined_until(self, source_id):
        return self._until if source_id in self.quarantined else None

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


def test_every_source_gets_exactly_one_poll_in_order():
    # The invariant the surfaces rely on: whatever happens to a source, it leaves exactly one
    # record, and the order is the order it was given. Before this, each fate went into its own
    # collection, so a render that iterated some of them dropped the others without a trace.
    quarantined = _FakeSource('quarantined', [_article('a1')])
    floored = _FakeSource('floored', [_article('a2')], due=False)
    failing = _FakeSource('failing', fail=True)
    healthy = _FakeSource('healthy', [_article('a3')])
    health = _FakeHealth(quarantined={'quarantined'}, until=_NOW)
    result = Ingestor([quarantined, floored, failing, healthy], _CountingEmbedder(),
                      _FakeStore(), health_store=health, source_set_id='crypto_news').run()

    assert [(poll.source_id, poll.status) for poll in result.polls] == [
        ('quarantined', 'quarantined'), ('floored', 'floor_skipped'),
        ('failing', 'failed'), ('healthy', 'ok')]
    # Only a polled source carries counters; a skip carries a reason instead.
    assert [poll.source_id for poll in result.polls if poll.ingest is not None] == ['healthy']
    assert all(poll.detail for poll in result.polls if poll.status != 'ok')
    assert result.polls[0].until == _NOW                   # the skip says when it ends


class _SuspendedEmbedder(AbstractEmbedder):
    """Stands in for the circuit-breaker gate (ISSUE_47): embedding is suspended (provider quota)."""

    def embed(self, texts: List[str]) -> EmbedResult:
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


# --- ISSUE_79: a poison article costs itself, not the pass ----------------------------------


class _RejectingEmbedder(AbstractEmbedder):
    """Refuses one text outright, as the provider does for an over-long input."""

    def __init__(self, reject_substring: str) -> None:
        self._reject = reject_substring

    def embed(self, texts: List[str]) -> EmbedResult:
        vectors: List[Optional[List[float]]] = []
        rejected: List[int] = []
        for index, text in enumerate(texts):
            if self._reject in text:
                vectors.append(None)
                rejected.append(index)
            else:
                vectors.append([float(len(text)), 0.0, 0.0, 0.0])
        return EmbedResult(
            vectors=vectors, rejected=rejected,
            input_tokens=[None if i in rejected else 7 for i in range(len(texts))],
            truncated_tokens=[None] * len(texts))


class _TruncatingEmbedder(AbstractEmbedder):
    """Reports one text as trimmed to the model's limit."""

    def embed(self, texts: List[str]) -> EmbedResult:
        return EmbedResult(
            vectors=[[1.0, 0.0, 0.0, 0.0] for _ in texts],
            input_tokens=[8192 if 'long' in text else 12 for text in texts],
            truncated_tokens=[4187 if 'long' in text else None for text in texts])


def test_a_rejected_article_does_not_cost_the_pass():
    """The 2026-08-11 regression, at the ingest seam.

    Before ISSUE_79 the provider's 400 propagated out of `run()` and the worker's blanket handler
    logged 'pass failed' — nothing from that source was stored, and the offender came back next
    pass because it had never been stored. Now it costs exactly itself.
    """
    source = _FakeSource('s1', [_article('good1'), _article('poison'), _article('good2')])
    result = Ingestor([source], _RejectingEmbedder('poison'), _FakeStore()).run()

    assert result.stored == 2                     # both good articles landed
    assert result.rejected == 1
    assert result.fetched == 3
    assert result.per_source['s1'].stored == 2
    assert result.polls[0].status == 'ok'         # the pass is a success, not a failure


def test_truncation_is_stamped_onto_the_article_and_counted():
    long_article = _article('long')
    source = _FakeSource('s1', [_article('short'), long_article])
    result = Ingestor([source], _TruncatingEmbedder(), _FakeStore()).run()

    assert result.truncated == 1
    assert result.embed_tokens == 8192 + 12       # what was actually sent, for the pass line
    assert long_article.embed_truncated_tokens == 4187   # the durable per-article record
    assert long_article.embed_input_tokens == 8192


def test_an_untouched_article_records_no_truncation():
    article = _article('short')
    Ingestor([_FakeSource('s1', [article])], _CountingEmbedder(), _FakeStore()).run()
    assert article.embed_truncated_tokens is None        # NULL in the corpus = nothing was cut
    assert article.embed_input_tokens is not None        # but the count is still recorded
