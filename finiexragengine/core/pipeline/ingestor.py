"""Ingest half of a pipeline: fetch -> embed only new -> idempotent upsert."""
from typing import List, Optional, Tuple

from finiexragengine.core.observability.source_health_store import (
    SourceHealthStore,
    normalize_host,
)
from finiexragengine.core.observability.stage_timer import StageTimer
from finiexragengine.core.pipeline.breaking_detector import BreakingDetector
from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.exceptions.ragengine_errors import BudgetExceededError, SourceFetchError
from finiexragengine.types.article_types import Article
from finiexragengine.types.ingest_types import IngestResult, SourceIngest


class Ingestor:
    """Runs the ingest pass over a pipeline's sources into the shared corpus.

    Per source: fetch -> ask the store which ids it already holds -> embed **only the
    new ones** -> idempotent upsert (ISSUE_3). **Store everything**; relevance is a
    retrieval-time decision, so the ingest side never filters on relevance. Skipping
    known ids before embedding matters: only the upsert is idempotent, so without the
    check a re-run would re-embed the whole feed window and pay for nothing. A single
    source failing degrades gracefully — recorded, the rest still ingest.

    This is the manual precursor to the scheduled ingest worker (ISSUE_10); the real
    staged `Pipeline.run` (ISSUE_7) calls the same pass as its first stage.
    """

    def __init__(self, sources: List[AbstractSource], embedder: AbstractEmbedder,
                 store: AbstractVectorStore,
                 breaking_detector: Optional[BreakingDetector] = None,
                 health_store: Optional[SourceHealthStore] = None,
                 source_set_id: str = '') -> None:
        self._sources = sources
        self._embedder = embedder
        self._store = store
        # Optional (ISSUE_11): flags breaking candidates cheaply after upsert. None = detection
        # off (e.g. a set with no interest in the breaking path); the ingest pass is unchanged.
        self._breaking_detector = breaking_detector
        # Optional (ISSUE_11): records every poll's health + drives the flag/quarantine policy.
        # None = health tracking off (manual CLI ingest, tests); the pass is otherwise unchanged.
        self._health_store = health_store
        self._source_set_id = source_set_id

    def run(self) -> IngestResult:
        """Fetch, embed only the new articles and upsert; return per-source + totals."""
        result = IngestResult()
        # Every stage is timed (ISSUE_32): one fetch/embed/upsert record per source; the
        # CLI footer aggregates them per stage, ISSUE_7 persists them with the envelope.
        timer = StageTimer()
        # Fresh articles + their vectors, accumulated across sources — breaking detection runs
        # once at the end so a story clustered across *different* feeds is visible (ISSUE_11).
        detect_batch: List[Tuple[Article, List[float]]] = []
        for source in self._sources:
            source_id = source.get_source_id()
            # 0. Skip a quarantined source (ISSUE_11) — it keeps failing (e.g. rate-limiting us),
            #    so we back off entirely until its cool-off elapses instead of hammering it.
            if self._health_store is not None and not self._health_store.should_poll(source_id):
                result.quarantined_skips.append(source_id)
                continue
            # 0b. Skip a source that is within its poll floor — a deliberate local no-op, so it is
            #     NOT recorded as a poll (a floor skip must never reset a failure streak).
            if not source.due_for_fetch():
                result.floor_skips.append(source_id)
                continue
            host = normalize_host(source.get_url())
            # 1. Pull the source. A failing source is recorded (typed, into health), the rest proceed.
            try:
                fetched = timer.time('fetch', source.fetch)
            except SourceFetchError as exc:
                result.failed_sources[source_id] = str(exc)
                if self._health_store is not None:
                    result.health_notes[source_id] = self._health_store.record_failure(
                        source_id, host, self._source_set_id,
                        error_type=exc.error_type, status=exc.status, message=str(exc))
                continue
            if self._health_store is not None:
                # A returned fetch (even empty / 304) means the source was reachable → success.
                if self._health_store.record_success(source_id, host, self._source_set_id):
                    result.recovered_sources.append(source_id)
            entry = SourceIngest(fetched=len(fetched))
            # 2. Skip ids already in the corpus — embedding a known article is wasted
            #    spend. (Sub-ms id lookup — deliberately untimed.)
            known = self._store.existing_ids([article.article_id for article in fetched])
            fresh = [article for article in fetched if article.article_id not in known]
            entry.embedded = len(fresh)
            if fresh:
                # 3. Embed the new article text once (title carries signal when the RSS
                #    summary is thin), then 4. idempotent upsert (rowcount = actually new).
                texts = [f'{article.title}. {article.summary}'.strip() for article in fresh]
                try:
                    vectors = timer.time('embed', lambda: self._embedder.embed(texts))
                except BudgetExceededError:
                    # Paid work suspended (provider quota, ISSUE_47): skip embedding this pass.
                    # Fetch + health already ran; the un-embedded articles reappear next pass, so
                    # nothing is lost while the feed window still holds them. Stop the pass here —
                    # every remaining source would suspend too.
                    result.suspended = True
                    entry.embedded = 0
                    result.per_source[source_id] = entry
                    result.fetched += entry.fetched
                    break
                entry.stored = timer.time('upsert', lambda: self._store.upsert(fresh, vectors))
                detect_batch.extend(zip(fresh, vectors))
            result.per_source[source_id] = entry
            result.fetched += entry.fetched
            result.embedded += entry.embedded
            result.stored += entry.stored
        # 5. Breaking detection (ISSUE_11) — LLM-free, over everything just stored, so
        #    cross-feed clusters count. Its highest tier drives the eval wake (Stage B).
        if self._breaking_detector is not None and detect_batch:
            articles, vectors = zip(*detect_batch)
            detection = self._breaking_detector.detect(list(articles), list(vectors))
            result.candidates = detection.candidates
            result.max_tier = detection.max_tier
        result.stage_timings = timer.timings
        return result
