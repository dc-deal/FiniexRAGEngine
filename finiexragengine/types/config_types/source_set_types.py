"""Pydantic config schema for a source-set — a named, shared group of feeds (ISSUE_10).

One file in configs/source_sets/ maps to one SourceSetConfig. Acquisition is the
source-set's concern: the ingest cadence lives here, next to the sources it clocks.
Constellations never own feeds — they reference a set by id (`source_set`), so one
set can feed N pipelines (crypto sentiment, fan variants, later market-wide moods)
with a single ingest worker: declare once, reference by id.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from finiexragengine.types.config_types.pipeline_config_types import TriggerConfig


class SourceConfig(BaseModel):
    """One feed inside a source-set (moved here from the constellation, ISSUE_10)."""
    source_id: str
    type: Literal['rss', 'blog', 'socket', 'api'] = 'rss'
    url: str
    weight: float = 1.0          # source trust / weight (ISSUE_5)
    # Declared but switched off: the feed keeps its entry (url, weight, comment) and is never
    # built or polled — same idiom as a disabled model variant. Reachability is often an
    # *environment* fact (a feed behind a bot-wall answers a datacenter IP with 403 and a clean
    # IP with 200), so the natural place to flip this is the per-machine
    # `user_configs/source_sets/` override — not the tracked catalogue. A disabled source is
    # invisible downstream: it counts in neither envelope reach number nor the error list —
    # switching a feed off is a decision, not a degradation. Operator-facing surfaces are the
    # opposite: the ingest report, feed doctor and Sources report all mark it `[disabled]`.
    enabled: bool = True
    # Editorial knowledge about the feed — JSON has no comments, so this is the sanctioned place
    # to record what we learned ("high-trust FX source", "behind Cloudflare from datacenter IPs").
    # It travels with the entry and can be patched per environment via the override.
    comment: Optional[str] = None
    # Optional per-source poll floor for continuous ingest (ISSUE_11): a genuinely slow
    # feed may opt out of the fast loop. None = polled every pass. Central-bank feeds are
    # NOT down-rated here — they are prime flash-crash sources; politeness comes from the
    # conditional GET (304), not from throttling.
    poll_interval_seconds: Optional[int] = None


class DetectionConfig(BaseModel):
    """Breaking-candidate detection thresholds (ISSUE_11) — source-set-scoped.

    Detection runs LLM-free at ingest over the *shared* corpus: a burst of near-duplicate
    articles across feeds (cluster size / velocity) is the primary signal; a keyword hit on a
    high-trust source is a secondary fast-path. Clustering happens across a set's feeds and the
    keyword vocabulary is market-specific, so the config lives with the source-set. Sensitivity
    (which tier wakes a given pipeline) is per-pipeline instead — see `BreakingConfig`.
    """
    cluster_similarity: float = 0.85     # pairwise cosine to count as the same story
    cluster_window_minutes: int = 60     # burst window
    mid_cluster_size: int = 3            # >= this many feeds carrying it -> importance MID (2)
    high_cluster_size: int = 5           # >= this (OR high-weight source + keyword) -> HIGH (3) + candidate
    keyword_source_weight: float = 0.9   # a source at/above this weight + a keyword hit alone -> HIGH
    # Static seed vocabulary (ISSUE_46 later auto-refreshes this field via an LLM flow — the
    # detector reads the same field, so seeding by hand now is zero rework).
    keywords: List[str] = Field(default_factory=list)


class SourceSetConfig(BaseModel):
    source_set_id: str
    # NOTE: `sources` is the declared catalogue; `active_sources()` below is what actually runs.
    # Ingest cadence — deliberately faster than eval (RSS windows slide; a missed
    # article is gone forever) and LLM-free, so frequent is cheap. For near-continuous
    # ingest (ISSUE_11 flash-crash latency) set this low (e.g. 15s); conditional GET keeps
    # fast polling cheap + polite.
    trigger: TriggerConfig = Field(
        default_factory=lambda: TriggerConfig(interval_seconds=300))
    detection: DetectionConfig = Field(default_factory=DetectionConfig)   # ISSUE_11
    sources: List[SourceConfig]

    def active_sources(self) -> List[SourceConfig]:
        """The sources that actually run — the declared catalogue minus the switched-off ones.

        The one definition of "active": the feeds the ingestor builds and the population
        `SourceReach` takes its census over both read it, so the set that runs and the set that
        is reported can never drift apart. A disabled feed must appear in *neither* envelope
        number: counting it would claim a contribution that does not exist — the whole catalogue
        would read 8/8 while seven feeds fed the signal.
        """
        return [source for source in self.sources if source.enabled]
