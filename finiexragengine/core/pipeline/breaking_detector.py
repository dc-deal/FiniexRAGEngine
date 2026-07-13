"""Breaking-candidate detection at ingest — LLM-free cluster-burst + keyword heuristic (ISSUE_11)."""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Pattern

from finiexragengine.core.rag.abstract_vector_store import AbstractVectorStore
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.source_set_types import DetectionConfig

logger = logging.getLogger(__name__)

# Importance tiers written to the corpus (ISSUE_11) — the graded signal the per-pipeline wake
# filter (breaking.min_importance) and the deep retrieval tier (importance >= 2) both read.
LOW, MID, HIGH = 1, 2, 3


@dataclass
class DetectionResult:
    """What one detection pass flagged — totals for the ingest log + the wake signal."""
    candidates: int = 0          # articles raised to HIGH (breaking_candidate = TRUE)
    mid: int = 0                 # articles raised to MID
    max_tier: int = 0            # highest tier written this pass (0 = nothing) — drives the wake


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

    def __init__(self, store: AbstractVectorStore, config: DetectionConfig) -> None:
        self._store = store
        self._config = config
        # Word-boundary match (not naive substring): "SEC" must not fire on "seconds", and a
        # phrase like "rate decision" matches as a unit. None when no keywords are configured.
        self._keyword_pattern: Optional[Pattern] = None
        if config.keywords:
            alternation = '|'.join(re.escape(keyword) for keyword in config.keywords)
            self._keyword_pattern = re.compile(rf'\b(?:{alternation})\b', re.IGNORECASE)

    def detect(self, fresh: List[Article], vectors: List[List[float]]) -> DetectionResult:
        """Score every fresh article for breaking; flag the ones that cross a tier."""
        result = DetectionResult()
        if not fresh:
            return result
        cfg = self._config
        # pgvector <=> is cosine *distance* (1 - similarity); a cluster member sits within this.
        max_distance = 1.0 - cfg.cluster_similarity
        since = datetime.now(timezone.utc) - timedelta(minutes=cfg.cluster_window_minutes)
        high_examples = []   # (title, cluster_size) — a few, to judge detection quality in the log
        for article, vector in zip(fresh, vectors):
            # Cluster size = near-duplicates already in the corpus within the window (this article
            # and its just-stored siblings included) — one COUNT(*), no rows, no LLM.
            cluster_size = self._store.count_neighbors(vector, since, max_distance)
            tier = self._tier(cluster_size, article.source_weight, self._has_keyword(article))
            if tier is None:
                continue   # routine article — left untagged (NULL importance)
            breaking = tier == HIGH
            self._store.flag_candidates([article.article_id], tier, breaking)
            if breaking:
                result.candidates += 1
                if len(high_examples) < 3:
                    high_examples.append((article.title, cluster_size))
            else:
                result.mid += 1
            result.max_tier = max(result.max_tier, tier)
        if result.max_tier:
            logger.info('[breaking] flagged %d HIGH + %d MID (window %dmin, sim>=%.2f)',
                        result.candidates, result.mid, cfg.cluster_window_minutes,
                        cfg.cluster_similarity)
            # Sample the flagged HIGH stories so an overnight review can spot false positives.
            for title, size in high_examples:
                logger.info('[breaking]   HIGH: %r (cluster %d)', title[:72], size)
            if result.candidates > len(high_examples):
                logger.info('[breaking]   … +%d more HIGH', result.candidates - len(high_examples))
        return result

    def _tier(self, cluster_size: int, source_weight: float,
              keyword_hit: bool) -> Optional[int]:
        """Map cluster size + the keyword fast-path to an importance tier (or None = routine)."""
        cfg = self._config
        # HIGH: a big burst OR a breaking keyword from a source we trust (no wait for the cluster).
        if cluster_size >= cfg.high_cluster_size or (
                keyword_hit and source_weight >= cfg.keyword_source_weight):
            return HIGH
        if cluster_size >= cfg.mid_cluster_size:
            return MID
        return None

    def _has_keyword(self, article: Article) -> bool:
        if self._keyword_pattern is None:
            return False
        return self._keyword_pattern.search(f'{article.title} {article.summary}') is not None
