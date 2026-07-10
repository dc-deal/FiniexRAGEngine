"""Ingest half of a pipeline: fetch -> embed only new -> idempotent upsert."""
from dataclasses import dataclass, field
from typing import Dict, List

from finiexragengine.core.observability.stage_timer import StageTimer
from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.exceptions.ragengine_errors import SourceFetchError
from finiexragengine.types.outcome_types import StageTiming


@dataclass
class SourceIngest:
    """One source's contribution to an ingest pass."""
    fetched: int = 0                # articles pulled from the feed
    embedded: int = 0               # articles sent to the embedder (the paid call)
    stored: int = 0                 # newly stored (upsert rowcount — genuinely new ids)

    @property
    def duplicates(self) -> int:
        """Fetched items already in the corpus (skipped, never re-embedded)."""
        return self.fetched - self.stored


@dataclass
class IngestResult:
    """What one ingest pass did — totals plus a per-source breakdown."""
    fetched: int = 0
    embedded: int = 0               # total paid embeddings this pass
    stored: int = 0
    per_source: Dict[str, SourceIngest] = field(default_factory=dict)
    failed_sources: Dict[str, str] = field(default_factory=dict)   # source_id -> error message
    stage_timings: List[StageTiming] = field(default_factory=list)  # fetch/embed/upsert per source (ISSUE_32)

    @property
    def duplicates(self) -> int:
        return self.fetched - self.stored


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
                 store: AbstractVectorStore) -> None:
        self._sources = sources
        self._embedder = embedder
        self._store = store

    def run(self) -> IngestResult:
        """Fetch, embed only the new articles and upsert; return per-source + totals."""
        result = IngestResult()
        # Every stage is timed (ISSUE_32): one fetch/embed/upsert record per source; the
        # CLI footer aggregates them per stage, ISSUE_7 persists them with the envelope.
        timer = StageTimer()
        for source in self._sources:
            source_id = source.get_source_id()
            # 1. Pull the source. A failing source is recorded, the rest proceed.
            try:
                fetched = timer.time('fetch', source.fetch)
            except SourceFetchError as exc:
                result.failed_sources[source_id] = str(exc)
                continue
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
                vectors = timer.time('embed', lambda: self._embedder.embed(texts))
                entry.stored = timer.time('upsert', lambda: self._store.upsert(fresh, vectors))
            result.per_source[source_id] = entry
            result.fetched += entry.fetched
            result.embedded += entry.embedded
            result.stored += entry.stored
        result.stage_timings = timer.timings
        return result
