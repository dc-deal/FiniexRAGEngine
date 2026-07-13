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
    # Ingest cadence — deliberately faster than eval (RSS windows slide; a missed
    # article is gone forever) and LLM-free, so frequent is cheap. For near-continuous
    # ingest (ISSUE_11 flash-crash latency) set this low (e.g. 15s); conditional GET keeps
    # fast polling cheap + polite.
    trigger: TriggerConfig = Field(
        default_factory=lambda: TriggerConfig(interval_seconds=300))
    detection: DetectionConfig = Field(default_factory=DetectionConfig)   # ISSUE_11
    sources: List[SourceConfig]
