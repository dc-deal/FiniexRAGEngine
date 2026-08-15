"""Ingest-side domain types — what one acquisition pass produced and how its sources fared.

The shapes the ingest flow hands across units: the `Ingestor` fills them, the `IngestWorker`
logs them, the `PipelineRunner` folds them into the envelope, and the ingest CLI prints them.
Behaviour lives in `core/pipeline/` and `core/observability/`; only the shapes live here.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from finiexragengine.types.outcome_types import StageTiming

# What the *poll itself* did — the only outcomes the journal records (ISSUE_76), because they are
# the only ones with a duration to measure. `suspended` is deliberately absent: the fetch of a
# suspended pass succeeded, and the quota stopped the embed stage one step later.
PollOutcome = Literal['ok', 'failed']
# What became of one source in one pass. `ok` and `suspended` were polled (they carry counters);
# the rest never reached the feed — `failed` tried and could not, `quarantined` and `floor_skipped`
# were deliberately not tried.
PollStatus = Literal[PollOutcome, 'quarantined', 'floor_skipped', 'suspended']


@dataclass
class SourceIngest:
    """One source's contribution to an ingest pass."""
    fetched: int = 0                # articles pulled from the feed
    embedded: int = 0               # articles the embedder returned a vector for (the paid call)
    stored: int = 0                 # newly stored (upsert rowcount — genuinely new ids)
    # What the embed stage did with them (ISSUE_79): how many had to be trimmed to the model's
    # input limit, how many the provider refused outright (dropped from this pass, never stored),
    # and the token total actually sent — the number that carries signal where the embedding
    # cost rounds to $0.000000 on every quiet pass.
    truncated: int = 0
    rejected: int = 0
    embed_tokens: int = 0

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
class PollSample:
    """One attempted poll, as the diagnostic journal records it (ISSUE_76).

    The sibling of a `cost_log` row for the *unpaid* calls: captured at the call, reported from
    the store. `duration_ms` is the reason it exists — it is measured on the failure path too,
    where `StageTimer` records nothing, so the polls most worth studying stop being invisible.

    Distinct from `SourcePoll`, which is what a *surface* renders for one pass (counters, a
    ready-to-display detail string). This is what a *time series* needs: narrow, comparable
    fields with no rendering in them.
    """
    source_id: str
    source_set: str
    outcome: PollOutcome
    duration_ms: float
    error_type: Optional[str] = None    # RunError taxonomy on failure, None on success
    status: Optional[int] = None        # HTTP status where the source knows one
    articles: int = 0                   # articles the fetch returned (0 on a 304)


@dataclass
class HealthOutcome:
    """What a failure record did — lets the worker pick a log level (denoise repeats)."""
    consecutive_failures: int
    just_flagged: bool          # this failure crossed the threshold -> newly quarantined
    quarantined_until: Optional[datetime]


@dataclass
class SourceHealthState:
    """One source's health as a *reach decision* needs it — not as a report renders it.

    Deliberately not the Sources report's row: that one carries fourteen display fields (host,
    totals, the event log). These four are what decides whether a feed is delivering, and the
    engine must not import from `reports/` anyway. The overlap is incidental, not structural.
    """
    source_id: str
    consecutive_failures: int
    quarantined_until: Optional[datetime] = None
    last_error_type: Optional[str] = None
    last_status: Optional[int] = None

    @property
    def delivering(self) -> bool:
        """Not in cool-off and its last poll succeeded — the definition of "reached"."""
        return (self.consecutive_failures == 0
                and (self.quarantined_until is None
                     or self.quarantined_until <= datetime.now(timezone.utc)))


@dataclass
class UnreachedSource:
    """A configured source that is not delivering, and why — the envelope's degradation trail."""
    source_id: str
    reason: str          # ready to display: the envelope preserves it, the Sources report does not


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
    unreached: List[UnreachedSource] = field(default_factory=list)   # the gap, named and explained


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
    truncated: int = 0              # inputs trimmed to the model's limit (ISSUE_79)
    rejected: int = 0               # inputs the provider refused — dropped, never stored
    embed_tokens: int = 0           # tokens actually sent to the embedder this pass
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
