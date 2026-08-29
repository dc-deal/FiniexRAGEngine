"""Ingest half of a pipeline: fetch -> embed only new -> idempotent upsert."""
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Dict, List, Optional, Tuple

from finiexragengine.core.observability.source_health_store import SourceHealthStore
from finiexragengine.core.observability.source_poll_log import SourcePollLog
from finiexragengine.core.observability.stage_timer import StageTimer
from finiexragengine.core.pipeline.breaking_detector import BreakingDetector
from finiexragengine.core.rag.abstract_embedder import AbstractEmbedder
from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.core.sources.abstract_source import AbstractSource
from finiexragengine.exceptions.ragengine_errors import BudgetExceededError, SourceFetchError
from finiexragengine.types.article_types import Article
from finiexragengine.types.ingest_types import (
    IngestResult,
    PollSample,
    SourceIngest,
    SourcePoll,
)
from finiexragengine.utils.url import normalize_host

logger = logging.getLogger(__name__)


@dataclass
class _FetchOutcome:
    """One source's pull, carried from the fetch phase to the accounting phase (ISSUE_107).

    File-private and deliberately not in `types/`: it never crosses a seam — it exists between
    `_fetch_all` and `_run_pass`, both inside this module. Holds the duration on **both** paths,
    because a fetch that raised is the one worth measuring (ISSUE_76).
    """
    started_at: datetime
    duration_ms: float
    articles: List[Article] = field(default_factory=list)
    error: Optional[SourceFetchError] = None


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
                 source_set_id: str = '',
                 poll_log: Optional[SourcePollLog] = None,
                 fetch_workers: int = 1) -> None:
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
        # Optional (ISSUE_76): the diagnostic journal — one row per poll attempt, with the duration
        # measured on the failure path too. None = journaling off (manual CLI ingest, tests, or the
        # config kill switch); the pass is otherwise unchanged.
        self._poll_log = poll_log
        # How many feeds this pass fetches at once (ISSUE_107, `SourceSetConfig.fetch_workers`).
        # 1 = the historical sequential pass. Only the fetch is affected; see `_fetch_all`.
        self._fetch_workers = max(1, fetch_workers)

    def _journal(self, sample: PollSample) -> None:
        """Record one poll attempt in the diagnostic journal, when one is attached."""
        if self._poll_log is not None:
            self._poll_log.record(sample)

    def run(self) -> IngestResult:
        """Fetch, embed only the new articles and upsert; return per-source + totals."""
        # The health decisions of this pass are resolved together at the end (ISSUE_84): whether
        # a crossed failure threshold means "this feed is broken" or "our connectivity is gone"
        # depends on how the *other* sources fared, which is not knowable inside the loop. The
        # counters are still written per failure, so a pass that dies mid-way loses no accounting.
        if self._health_store is None:
            return self._run_pass()
        with self._health_store.pass_scope(self._source_set_id):
            result = self._run_pass()
        # Read after the scope closed — that is when the correlated verdict exists.
        result.host_event = self._health_store.take_host_event()
        return result

    def _plan_pass(self) -> List[Tuple[AbstractSource, Optional[SourcePoll]]]:
        """Decide, in declared order, who is polled — and record a `SourcePoll` for who is not.

        Deliberately its own sequential phase (ISSUE_107): it reads the shared quarantine state and
        hands out at most one half-open probe per source, so it must never run concurrently with
        itself. A `None` poll means "due — fetch it".
        """
        plan: List[Tuple[AbstractSource, Optional[SourcePoll]]] = []
        for source in self._sources:
            source_id = source.get_source_id()
            # Every branch below records exactly one SourcePoll — that is what makes "every source
            # appears in the render" structural rather than a thing each surface has to remember.
            # 0. Skip a quarantined source (ISSUE_11) — it keeps failing (e.g. rate-limiting us),
            #    so we back off entirely until its cool-off elapses instead of hammering it.
            #    The same check also holds the whole set during a connectivity back-off
            #    (ISSUE_84), which is a different fact and gets its own status: the feed did
            #    nothing wrong, so a surface must not point the operator at it.
            if self._health_store is not None and not self._health_store.should_poll(source_id):
                host_until = self._health_store.host_backoff_until()
                if host_until is not None:
                    plan.append((source, SourcePoll(
                        source_id, 'host_backoff', until=host_until,
                        detail='set-wide back-off — local connectivity failure, not this feed')))
                    continue
                until = self._health_store.quarantined_until(source_id)
                # Carry the rung so every surface can say "wait an hour" and "this feed is
                # effectively gone" differently — one word apart today.
                rung = self._health_store.rung_of(source_id)
                ladder = f' (rung {rung[0] + 1}/{rung[1]})' if rung else ''
                plan.append((source, SourcePoll(
                    source_id, 'quarantined', until=until, rung=rung,
                    detail=f'in source-health cool-off after repeated failures{ladder}')))
                continue
            # 0b. Skip a source that is within its poll floor — a deliberate local no-op, so it is
            #     NOT recorded as a poll (a floor skip must never reset a failure streak).
            if not source.due_for_fetch():
                plan.append((source, SourcePoll(source_id, 'floor_skipped',
                                                detail='within its own poll floor')))
                continue
            plan.append((source, None))
        return plan

    def _fetch_all(self, due: List[AbstractSource],
                   timer: StageTimer) -> Dict[str, _FetchOutcome]:
        """Pull every due source — pooled when the set asks for it (ISSUE_107).

        The ONLY phase that runs concurrently, and the reason the split is drawn here: a fetch
        touches the network and its own source object, nothing shared. Everything that mutates
        shared state (health, journal, budget, corpus, timings) stays in the sequential phase, so
        parallelism cannot reorder an accounting decision.

        Durations are measured per source on both paths (ISSUE_76 — a fetch that times out is
        precisely the one worth measuring) and land in the poll journal either way. What differs is
        the StageTimer: pooled fetches overlap, so per-source records would sum to several times the
        pass's own wall clock and the perf report would claim a pass spent longer fetching than it
        existed. Pooled, the phase therefore contributes ONE wall-clock `fetch` record; sequential,
        the per-source records are additive and are kept as they always were.
        """
        outcomes: Dict[str, _FetchOutcome] = {}

        def pull(source: AbstractSource) -> _FetchOutcome:
            started = datetime.now(timezone.utc)
            start = perf_counter()
            try:
                articles = source.fetch()
            except SourceFetchError as exc:
                return _FetchOutcome(started, (perf_counter() - start) * 1000.0, error=exc)
            return _FetchOutcome(started, (perf_counter() - start) * 1000.0, articles=articles)

        if self._fetch_workers > 1 and len(due) > 1:
            phase_started = datetime.now(timezone.utc)
            phase_start = perf_counter()
            with ThreadPoolExecutor(max_workers=min(self._fetch_workers, len(due)),
                                    thread_name_prefix='fetch') as pool:
                for source, outcome in zip(due, pool.map(pull, due)):
                    outcomes[source.get_source_id()] = outcome
            timer.record('fetch', phase_started, (perf_counter() - phase_start) * 1000.0)
            return outcomes
        for source in due:
            outcome = pull(source)
            outcomes[source.get_source_id()] = outcome
            timer.record('fetch', outcome.started_at, outcome.duration_ms)
        return outcomes

    def _run_pass(self) -> IngestResult:
        """One acquisition pass over every source of the set.

        Three phases (ISSUE_107): plan who is polled, fetch them (possibly concurrently), then walk
        the plan in declared order and do everything that costs money or mutates state. The result
        object is identical to the single-loop form it replaced — same polls, same order.
        """
        result = IngestResult()
        # Every stage is timed (ISSUE_32): one fetch/embed/upsert record per source; the
        # CLI footer aggregates them per stage, ISSUE_7 persists them with the envelope.
        timer = StageTimer()
        # Fresh articles + their vectors, accumulated across sources — breaking detection runs
        # once at the end so a story clustered across *different* feeds is visible (ISSUE_11).
        detect_batch: List[Tuple[Article, List[float]]] = []
        plan = self._plan_pass()
        due = [source for source, poll in plan if poll is None]
        fetched_by_id = self._fetch_all(due, timer)
        for index, (source, planned) in enumerate(plan):
            source_id = source.get_source_id()
            if planned is not None:
                result.polls.append(planned)
                continue
            host = normalize_host(source.get_url())
            # 1. Account for the pull. A failing source is recorded (typed, into health), the rest
            #    proceed.
            outcome = fetched_by_id[source_id]
            fetch_ms = outcome.duration_ms
            if outcome.error is not None:
                exc = outcome.error
                self._journal(PollSample(source_id, self._source_set_id, 'failed', fetch_ms,
                                         error_type=exc.error_type, status=exc.status))
                result.polls.append(SourcePoll(source_id, 'failed', detail=str(exc)))
                if self._health_store is not None:
                    # The duration and the source's own deadline ride along (ISSUE_84): they are
                    # what splits the overloaded UNREACHABLE bucket into "went quiet" and
                    # "refused", and therefore what picks the cool-off.
                    result.health_notes[source_id] = self._health_store.record_failure(
                        source_id, host, self._source_set_id,
                        error_type=exc.error_type, status=exc.status, message=str(exc),
                        duration_ms=fetch_ms,
                        deadline_ms=source.get_fetch_deadline_ms())
                continue
            fetched = outcome.articles
            self._journal(PollSample(source_id, self._source_set_id, 'ok', fetch_ms,
                                     articles=len(fetched)))
            if self._health_store is not None:
                # A returned fetch (even empty / 304) means the source was reachable → success.
                if self._health_store.record_success(source_id, host, self._source_set_id):
                    result.recovered_sources.append(source_id)
            entry = SourceIngest(fetched=len(fetched))
            # 1b. What the normaliser removed on the way in (ISSUE_112). Derived from the raw
            #     fields rather than counted in the source: an article carrying either raw field is
            #     one whose text changed, so the record IS the counter and nothing has to be
            #     threaded back through `fetch`. Counted over everything fetched, not only the new
            #     ids — the number answers "how dirty is this feed", which is a property of the
            #     pull, and it sits next to `fetched` for exactly that reason.
            entry.normalised = sum(1 for article in fetched
                                   if article.title_raw is not None
                                   or article.summary_raw is not None)
            entry.dropped_chars = sum(
                len(article.title_raw or article.title) - len(article.title)
                + len(article.summary_raw or article.summary) - len(article.summary)
                for article in fetched)
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
                    embedded = timer.time('embed', lambda: self._embedder.embed(texts))
                except BudgetExceededError:
                    # Paid work suspended (provider quota, ISSUE_47): skip embedding this pass.
                    # Fetch + health already ran; the un-embedded articles reappear next pass, so
                    # nothing is lost while the feed window still holds them. Stop the pass here —
                    # every remaining source would suspend too. The sources after this one get no
                    # poll entry at all, which is honest: they were never accounted for. The report
                    # renders them from the declared catalogue as 'not polled'.
                    result.suspended = True
                    entry.embedded = 0
                    result.polls.append(SourcePoll(
                        source_id, 'suspended', ingest=entry,
                        detail='paid work suspended (provider quota) — fetched, not embedded'))
                    result.fetched += entry.fetched
                    result.normalised += entry.normalised
                    result.dropped_chars += entry.dropped_chars
                    # "Reappear next pass" is the invariant this whole branch rests on, and
                    # pre-fetching would have quietly broken it (ISSUE_107): a successful fetch
                    # advances the feed's ETag/Last-Modified, so a source pulled here but never
                    # embedded would answer 304 next pass and its articles would be lost for good —
                    # a loss the sequential form could not produce, because it never reached them.
                    # Rewinding the validators makes the next pass re-pull them for real.
                    for pending, pending_poll in plan[index + 1:]:
                        if pending_poll is None:
                            pending.reset_conditional_get()
                    break
                # Stamp what the embedding actually saw onto each article, then keep only the ones
                # the provider accepted (ISSUE_79). A rejected item is dropped from this pass
                # rather than taking the batch — and every other article still lands.
                storable: List[Article] = []
                storable_vectors: List[List[float]] = []
                for position, article in enumerate(fresh):
                    vector = embedded.vectors[position]
                    if vector is None:
                        continue
                    article.embed_input_tokens = embedded.input_tokens[position]
                    article.embed_truncated_tokens = embedded.truncated_tokens[position]
                    storable.append(article)
                    storable_vectors.append(vector)
                entry.embedded = len(storable)
                entry.truncated = embedded.truncated_count
                entry.rejected = len(embedded.rejected)
                entry.embed_tokens = embedded.counted_tokens
                # A rejected article leaves no row to mark, so it is called out here and counted on
                # the pass line. Deliberately NOT routed through `source_health.record_failure`:
                # that records a *poll* outcome — it would bump this source's consecutive-failure
                # streak and quarantine a perfectly reachable feed for shipping one long article,
                # losing all of its articles. Worse than the defect being fixed. The per-source
                # view belongs to the article columns (ISSUE_76 aggregates them).
                if embedded.rejected:
                    logger.warning(
                        '[%s] %d article(s) refused by the embedding provider even after fitting '
                        'to the input limit — stored %d of %d', source_id,
                        len(embedded.rejected), len(storable), len(fresh))
                entry.stored = timer.time(
                    'upsert', lambda: self._store.upsert(storable, storable_vectors))
                detect_batch.extend(zip(storable, storable_vectors))
            result.polls.append(SourcePoll(source_id, 'ok', ingest=entry))
            result.fetched += entry.fetched
            result.embedded += entry.embedded
            result.truncated += entry.truncated
            result.rejected += entry.rejected
            result.embed_tokens += entry.embed_tokens
            result.normalised += entry.normalised
            result.dropped_chars += entry.dropped_chars
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
