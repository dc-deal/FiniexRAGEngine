"""Ingest-side domain types — what one acquisition pass produced and how its sources fared.

The shapes the ingest flow hands across units: the `Ingestor` fills them, the `IngestWorker`
logs them, the `PipelineRunner` folds them into the envelope, and the ingest CLI prints them.
Behaviour lives in `core/pipeline/` and `core/observability/`; only the shapes live here.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

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
class HealthOutcome:
    """What a failure record did — lets the worker pick a log level (denoise repeats)."""
    consecutive_failures: int
    just_flagged: bool          # this failure crossed the threshold -> newly quarantined
    quarantined_until: Optional[datetime]


@dataclass
class DetectionResult:
    """What one detection pass flagged — totals for the ingest log + the wake signal."""
    candidates: int = 0          # articles raised to HIGH (breaking_candidate = TRUE)
    mid: int = 0                 # articles raised to MID
    max_tier: int = 0            # highest tier written this pass (0 = nothing) — drives the wake


@dataclass
class IngestResult:
    """What one ingest pass did — totals plus a per-source breakdown."""
    fetched: int = 0
    embedded: int = 0               # total paid embeddings this pass
    stored: int = 0
    candidates: int = 0             # breaking candidates flagged this pass (HIGH tier, ISSUE_11)
    max_tier: int = 0               # highest importance tier written this pass — drives the eval wake (ISSUE_11)
    suspended: bool = False         # paid embedding suspended this pass (provider quota, ISSUE_47)
    per_source: Dict[str, SourceIngest] = field(default_factory=dict)
    failed_sources: Dict[str, str] = field(default_factory=dict)   # source_id -> error message
    # Source-health outcomes for this pass (ISSUE_11) — let the worker pick a log level so
    # repeated identical failures are denoised (WARN once, DEBUG the repeats, WARN on flag).
    health_notes: Dict[str, HealthOutcome] = field(default_factory=dict)   # per failed source
    quarantined_skips: List[str] = field(default_factory=list)     # sources skipped (in quarantine)
    floor_skips: List[str] = field(default_factory=list)           # sources skipped (within poll floor)
    recovered_sources: List[str] = field(default_factory=list)     # sources that came back this pass
    stage_timings: List[StageTiming] = field(default_factory=list)  # fetch/embed/upsert per source (ISSUE_32)

    @property
    def duplicates(self) -> int:
        return self.fetched - self.stored
