"""Breaking-candidate detection at ingest — LLM-free cluster-burst + keyword heuristic (ISSUE_11)."""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Pattern, Set, Tuple

from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.types.article_types import Article, NeighbourCount
from finiexragengine.types.config_types.source_set_types import DetectionConfig
from finiexragengine.types.ingest_types import DetectionResult, DetectionTrigger

logger = logging.getLogger(__name__)

# Importance tiers written to the corpus (ISSUE_11) — the graded signal the per-pipeline wake
# filter (breaking.min_importance) and the deep retrieval tier (importance >= 2) both read.
LOW, MID, HIGH = 1, 2, 3

# The two detection paths, named once (ISSUE_106) — the value reaches a corpus column and a report,
# so it is worth a constant rather than a literal repeated at four call sites.
CLUSTER: DetectionTrigger = 'cluster'
KEYWORD: DetectionTrigger = 'keyword'


@dataclass
class _TierVerdict:
    """A tier and the path that reached it (ISSUE_106).

    File-private and deliberately not in `types/`: it never crosses a seam — `_tier` builds it and
    `detect` consumes it, both inside this class. A result object rather than a tuple, because the
    next thing anyone wants here is the cluster size that justified the tier.
    """
    tier: int
    trigger: DetectionTrigger


class BreakingDetector:
    """Flags breaking candidates cheaply at ingest — no LLM (ISSUE_11).

    Primary signal — **cluster-burst**: the same story hitting many feeds in a short window forms
    a tight embedding cluster; the cluster size *is* the breaking signal (the near-duplicate
    dedup we already do, read as a count). Secondary fast-path — a **keyword** hit on a high-trust
    source flags HIGH on its own, without waiting for the cluster to build.

    Runs *after* upsert with the fresh articles + their vectors, so `count_neighbors` sees every
    copy just stored (cross-feed clusters included). Writes the graded importance tier + the
    breaking-candidate flag onto the flagged articles via `flag_candidates`. Pure vector math +
    string match — no LLM call, ever. The highest tier written drives the eval wake (Stage B).
    """

    def __init__(self, store: AbstractVectorStore, config: DetectionConfig,
                 source_ids: Optional[Set[str]] = None) -> None:
        self._store = store
        self._config = config
        # The feeds this set actually runs (ISSUE_106). The neighbour count is scoped to them, so a
        # macro story carried by another source-set no longer inflates this set's cluster size
        # against this set's thresholds. `None` counts corpus-wide — the pre-ISSUE_106 behaviour,
        # kept for a caller with no set in hand rather than as a default anyone should choose.
        self._source_ids = source_ids
        # Word-boundary match (not naive substring): "SEC" must not fire on "seconds", and a
        # phrase like "rate decision" matches as a unit. None when no keywords are configured.
        self._keyword_pattern: Optional[Pattern] = None
        # Surface form -> the CONFIGURED term (ISSUE_106). The pattern is case-insensitive, so a
        # match carries the text as the feed wrote it — 'Emergency' for a configured 'emergency'.
        # Recording that would split one term across two rows in `detection_quality`, so every hit
        # is mapped back to the spelling the operator declared before it is persisted.
        self._keyword_terms: Dict[str, str] = {}
        if config.keywords:
            alternation = '|'.join(re.escape(keyword) for keyword in config.keywords)
            self._keyword_pattern = re.compile(rf'\b(?:{alternation})\b', re.IGNORECASE)
            self._keyword_terms = {keyword.lower(): keyword for keyword in config.keywords}

    def detect(self, fresh: List[Article], vectors: List[List[float]]) -> DetectionResult:
        """Score every fresh article for breaking; flag the ones that cross a tier."""
        result = DetectionResult()
        if not fresh:
            return result
        cfg = self._config
        # pgvector <=> is cosine *distance* (1 - similarity); a cluster member sits within this.
        max_distance = 1.0 - cfg.cluster_similarity
        since = datetime.now(timezone.utc) - timedelta(minutes=cfg.cluster_window_minutes)
        # (title, cluster_size, terms) — a few, to judge detection quality in the log. The terms
        # ride along because `GET /v1/logs/engine` made the log remotely readable: a flag is then
        # traceable to the word that made it without waiting for a report to be run.
        high_examples: List[Tuple[str, int, Tuple[str, ...]]] = []
        for article, vector in zip(fresh, vectors):
            # The neighbourhood already in the corpus within the window (this article and its
            # just-stored siblings included) — one query, no rows materialized, no LLM. Skipped
            # entirely when the cluster path is switched off (ISSUE_106): a set with nothing to
            # find should not pay for the probe, and the keyword fast-path below is unaffected.
            neighbours = (self._store.count_neighbors(vector, since, max_distance,
                                                      source_ids=self._source_ids)
                          if cfg.cluster_enabled else NeighbourCount(articles=0, feeds=0))
            # Which number the tiers are read against is the set's choice (ISSUE_106): 'feeds'
            # counts distinct outlets — corroboration — while 'articles' counts near-duplicate
            # density, which one feed can reach on its own.
            cluster_size = (neighbours.feeds if cfg.cluster_unit == 'feeds'
                            else neighbours.articles)
            matched = self._matched_keywords(article)
            # `_tier` still takes a bool: which tier this is remains a decision, not an attribution,
            # so the terms travel to the store rather than into the tier logic.
            verdict = self._tier(cluster_size, article.source_weight, bool(matched))
            if verdict is None:
                continue   # routine article — left untagged (NULL importance)
            tier = verdict.tier
            breaking = tier == HIGH
            # The neighbourhood travels onto the row only when the cluster path produced the
            # verdict: a keyword flag leaves both columns NULL rather than claiming a cluster it
            # never consulted. NULL means "not measured", the same distinction `detection_trigger`
            # draws — and it is what lets `detection_quality` read the duplication ratio of the
            # flags the cluster path actually made.
            measured = neighbours if verdict.trigger == CLUSTER else None
            # The vocabulary travels by the mirror-image rule (migration 014): only when the KEYWORD
            # path produced the verdict. An article can match a term and still be flagged by the
            # cluster path — recording the terms there would credit a vocabulary that decided
            # nothing, which is the same false claim an empty array would make.
            fired = matched if verdict.trigger == KEYWORD else None
            self._store.flag_candidates([article.article_id], tier, breaking,
                                        trigger=verdict.trigger, neighbours=measured,
                                        keywords=fired)
            result.by_trigger[verdict.trigger] = result.by_trigger.get(verdict.trigger, 0) + 1
            if breaking:
                result.candidates += 1
                if len(high_examples) < 3:
                    high_examples.append((article.title, cluster_size, matched))
            else:
                result.mid += 1
            result.max_tier = max(result.max_tier, tier)
        if result.max_tier:
            # The per-path split rides the line (ISSUE_106): the persisted column is the durable
            # answer, this is the at-the-call echo — the same rule that puts spend on a pass line.
            split = ' · '.join(f'{trigger} {count}'
                               for trigger, count in sorted(result.by_trigger.items()))
            logger.info('[breaking] flagged %d HIGH + %d MID via %s (window %dmin, sim>=%.2f)',
                        result.candidates, result.mid, split or 'nothing',
                        cfg.cluster_window_minutes, cfg.cluster_similarity)
            # Sample the flagged HIGH stories so an overnight review can spot false positives.
            for title, size, terms in high_examples:
                # Named rather than counted: 'cluster 5' and "the word 'hack' appeared" are
                # different justifications, and the line that samples false positives should say
                # which one it is. No terms is the cluster path's own flag, not an empty match.
                evidence = f"keywords {', '.join(terms)}" if terms else f'cluster {size}'
                logger.info('[breaking]   HIGH: %r (%s)', title[:72], evidence)
            if result.candidates > len(high_examples):
                logger.info('[breaking]   … +%d more HIGH', result.candidates - len(high_examples))
        return result

    def _tier(self, cluster_size: int, source_weight: float,
              keyword_hit: bool) -> Optional[_TierVerdict]:
        """Map cluster size + the keyword fast-path to a tier AND the path that got it there.

        Returns a verdict rather than a bare tier (ISSUE_106): the two paths are near-independent
        channels, and which one fired is the fact every calibration question needs. It was known
        exactly here and discarded one line later, which is why `flagged_candidates` has only ever
        been the sum of both.
        """
        cfg = self._config
        # HIGH: a big burst OR a breaking keyword from a source we trust (no wait for the cluster).
        # The cluster is checked FIRST because that is the tier's primary meaning — when both would
        # fire, the burst is the stronger evidence and the one the threshold is calibrated on.
        # Attributing an overlap to the fast path would flatter the fast path's hit rate.
        if cluster_size >= cfg.high_cluster_size:
            return _TierVerdict(HIGH, CLUSTER)
        if keyword_hit and source_weight >= cfg.keyword_source_weight:
            return _TierVerdict(HIGH, KEYWORD)
        if cluster_size >= cfg.mid_cluster_size:
            return _TierVerdict(MID, CLUSTER)
        return None

    def _matched_keywords(self, article: Article) -> Tuple[str, ...]:
        """Every configured term this article matches — the evidence a keyword flag is made on.

        Returns the terms rather than a boolean (ISSUE_106, migration 014). The tier decision only
        needs "did anything match", but the *record* needs which: a vocabulary is tuned per term,
        and "the keyword path made 56 flags" is not a sentence anyone can act on.

        **All matches, not the first.** `search` returns the earliest match in *text* order, which
        bears no relation to the config — under first-match attribution a term that always
        co-occurs with another reads as never having fired, and that is exactly the question this
        is for. Deduped and sorted so the stored array is comparable between rows.
        """
        if self._keyword_pattern is None:
            return ()
        found = self._keyword_pattern.findall(f'{article.title} {article.summary}')
        # Back to the configured spelling: the pattern is case-insensitive, so `findall` hands back
        # the feed's own casing and the same term would otherwise split across rows in the report.
        return tuple(sorted({self._keyword_terms.get(hit.lower(), hit) for hit in found}))
