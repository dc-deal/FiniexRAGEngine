"""Ingest-side domain types — what one acquisition pass produced and how its sources fared.

The shapes the ingest flow hands across units: the `Ingestor` fills them, the `IngestWorker`
logs them, the `PipelineRunner` folds them into the envelope, and the ingest CLI prints them.
Behaviour lives in `core/pipeline/` and `core/observability/`; only the shapes live here.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional

from finiexragengine.types.outcome_types import StageTiming

# What became of one source in one pass. `ok` and `suspended` were polled (they carry counters);
# the rest never reached the feed — `failed` tried and could not, `quarantined` and `floor_skipped`
# were deliberately not tried.
PollStatus = Literal['ok', 'failed', 'quarantined', 'floor_skipped', 'suspended']


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
class SourcePoll:
    """What one pass did with one source — the record every ingest surface renders from."""
    source_id: str
    status: PollStatus
    ingest: Optional[SourceIngest] = None   # the counters — only a source that was polled has them
    detail: str = ''                        # error message / skip reason, ready to display
    until: Optional[datetime] = None        # when a deferred source becomes pollable again


@dataclass
class HealthOutcome:
    """What a failure record did — lets the worker pick a log level (denoise repeats)."""
    consecutive_failures: int
    just_flagged: bool          # this failure crossed the threshold -> newly quarantined
    quarantined_until: Optional[datetime]


@dataclass
class ReachCensus:
    """How many of a source-set's sources are live right now — config ∩ health.

    The envelope's `sources_configured` / `sources_reached` read this instead of deriving one
    from the other. Both numbers come from one place on purpose: they were computed at different
    layers and different times before (config at assembly, reach at run), so `reached` was a
    subtraction of failures from a count it could not check — and every non-failure way of missing
    a source (quarantine, an aborted pass, an eval that never fetched) was invisible by
    construction.
    """
    configured: int                              # the set's active (enabled) sources
    reached: int                                 # of those, the ones whose feed is delivering
    unreached: List[str] = field(default_factory=list)   # the ids behind the gap, for the trail


@dataclass
class DetectionResult:
    """What one detection pass flagged — totals for the ingest log + the wake signal."""
    candidates: int = 0          # articles raised to HIGH (breaking_candidate = TRUE)
    mid: int = 0                 # articles raised to MID
    max_tier: int = 0            # highest tier written this pass (0 = nothing) — drives the wake


@dataclass
class IngestResult:
    """What one ingest pass did — totals plus a per-source breakdown.

    `polls` is the single record of per-source outcome: one entry per source the pass
    considered, appended in config order. The dict/list views below are derived from it, never
    stored alongside — a source's fate used to be scattered across five parallel collections,
    and a surface that iterated only some of them (the ingest CLI printed two) dropped the rest
    silently. One ordered list means a skipped source cannot fall out of a render.
    """
    fetched: int = 0
    embedded: int = 0               # total paid embeddings this pass
    stored: int = 0
    candidates: int = 0             # breaking candidates flagged this pass (HIGH tier, ISSUE_11)
    max_tier: int = 0               # highest importance tier written this pass — drives the eval wake (ISSUE_11)
    suspended: bool = False         # paid embedding suspended this pass (provider quota, ISSUE_47)
    polls: List[SourcePoll] = field(default_factory=list)
    # Source-health outcomes for this pass (ISSUE_11) — let the worker pick a log level so
    # repeated identical failures are denoised (WARN once, DEBUG the repeats, WARN on flag).
    health_notes: Dict[str, HealthOutcome] = field(default_factory=dict)   # per failed source
    recovered_sources: List[str] = field(default_factory=list)     # sources that came back this pass
    stage_timings: List[StageTiming] = field(default_factory=list)  # fetch/embed/upsert per source (ISSUE_32)

    @property
    def duplicates(self) -> int:
        return self.fetched - self.stored

    @property
    def per_source(self) -> Dict[str, SourceIngest]:
        """The sources that were actually polled, with their counters."""
        return {poll.source_id: poll.ingest for poll in self.polls if poll.ingest is not None}

    @property
    def failed_sources(self) -> Dict[str, str]:
        """source_id -> error message, for the sources whose fetch raised."""
        return {poll.source_id: poll.detail for poll in self.polls if poll.status == 'failed'}

    @property
    def quarantined_skips(self) -> List[str]:
        """Sources not polled because source-health has them in cool-off."""
        return [poll.source_id for poll in self.polls if poll.status == 'quarantined']

    @property
    def floor_skips(self) -> List[str]:
        """Sources not polled because they are within their own poll floor."""
        return [poll.source_id for poll in self.polls if poll.status == 'floor_skipped']
