"""Edge-triggered breaking episodes (ISSUE_11 · ISSUE_82 · groundwork for ISSUE_9).

A hot story stays `is_breaking` across many eval passes; counting or pushing on every pass inflates
"confirmed" and lets the reaction time grow with the wall-clock (it keeps re-anchoring on ageing
context articles). An **episode** is instead counted once, on the transition *into* breaking — the
streaming twin of the batch grouping the store-based `breaking_report` does.

Where the episode boundary falls is decided by `BreakingEpisodeRule` (ISSUE_82), which both this
streaming tracker and the store report drive. This file keeps what is specific to the *live* path:
turning an opened episode into a `BreakingEpisode` with its frozen reaction time and reason.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from finiexragengine.core.pipeline.breaking_episode_rule import (
    BreakingEpisodeRule,
    EpisodeGrouping,
)
from finiexragengine.types.outcome_types import AnalysisEnvelope, SentimentResult


@dataclass
class BreakingEpisode:
    """One confirmed breaking episode — the start of a hot story, with its frozen reaction time."""
    symbol: str
    signal: str
    urgency: float
    engine_s: Optional[float]        # envelope ts − freshest source fetched_at (what we control)
    end_to_end_s: Optional[float]    # envelope ts − freshest REAL published_at (estimated excluded)
    n_sources: int
    # Why it broke (ISSUE_64 Phase 1): the LLM's own per-symbol `reasoning`, carried through so the
    # dashboard/report can show the trigger. Phase 2 replaces this with a dedicated `breaking_reason`.
    reason: str = ''


@dataclass
class BreakingPass:
    """What one envelope did to the episode state (ISSUE_82).

    A result object rather than the previous bare `List[BreakingEpisode]`: the pass now has a
    second thing to say — which symbols *held* an already-open episode without opening one. With
    hysteresis that is no longer the same as "was breaking", because a pass below the confirm gate
    but at or above the exit gate keeps the story alive. Lives next to `BreakingEpisode` because
    the two are one unit's shapes; the cross-seam decision shape is `types.eval_types`.
    """
    started: List[BreakingEpisode] = field(default_factory=list)
    held: List[str] = field(default_factory=list)     # symbols whose open episode this pass held


@dataclass
class OpenEpisode:
    """An episode still running after a replay — what a fresh process needs to keep showing it.

    The seeded rule knows an episode is open, but the live dashboard's episode list is written only
    by `add_breaking_episode`, so after a restart the panel read `none active` while a story was
    demonstrably still running (production, 2026-08-18). The state was correct and invisible, which
    on the one panel built to show breaking state is the worse half of the two.

    Carries the opening pass's `BreakingEpisode` (symbol, signal, reason, frozen reaction) plus the
    two timestamps the renderer needs: when it started, and when it was last held open.
    """
    episode: BreakingEpisode
    started: datetime
    last_seen: datetime
    # True when the episode was already open at the FIRST replayed envelope: the chain may reach
    # back further than the window, so `started` is a lower bound and every surface must say so.
    started_bounded: bool = False


def reaction_times(result: SentimentResult, ts: datetime) -> Tuple[Optional[float], Optional[float]]:
    """`(engine_s, end_to_end_s)` for one breaking result — e2e ignores estimated publish dates.

    Anchored on the **freshest** source, not the oldest (ISSUE_81). Anchoring on the oldest made
    this measure the retrieval window rather than any reaction: a pass retrieves context up to
    `recency_window_minutes` back (1440 = 24h), so `min(fetched_at)` reported ~21h in production
    for an engine that evaluates every 10 minutes and jumps the queue on a breaking wake in
    seconds. It was not merely inflated — it answered a different question ("how old is the oldest
    article we read?").

    `max` answers the intended one: how fresh was the evidence when the engine decided. Episodes
    are edge-triggered (see the tracker below), so the reaction is sampled only at the *start* of
    an episode — a later, unrelated article cannot drift the number afterwards, which is what
    makes the freshest-source anchor sound rather than merely less wrong.

    The precise anchor would be the article that actually triggered the detection
    (`articles.flagged_at`), but the envelope does not record *which* of its sources was flagged —
    the store report could join it and the live path could not, and the two must agree by
    construction. Carrying that flag on the envelope rides ISSUE_64 Phase 2, which extends it
    anyway. Note that a large class of episodes has no flagged article at all: the confirm gate
    fires on the LLM's urgency independently of detection (measured 2026-08-17, ISSUE_82).

    A date-less feed falls back to `published_at := fetched_at` (so recency filtering still works);
    those estimated dates would collapse e2e onto engine, so they are excluded from the e2e sample.
    None when no usable source timestamp exists (e2e then renders as `—`, honest).
    """
    fetched = [s.fetched_at for s in result.sources if s.fetched_at]
    published = [s.published_at for s in result.sources
                 if s.published_at and s.published_at != s.fetched_at]
    engine = (ts - max(fetched)).total_seconds() if fetched else None
    end_to_end = (ts - max(published)).total_seconds() if published else None
    return engine, end_to_end


class BreakingEpisodeTracker:
    """Streaming driver of `BreakingEpisodeRule`: which results START an episode this pass.

    One tracker per eval worker (a worker owns one pipeline's symbols). Its state is the rule's,
    and the rule is seeded at boot from the persisted envelopes (`pipeline_assembler`), so a
    restart no longer re-opens an ongoing story as a fresh episode — the divergence between this
    live view and the store report that ISSUE_82 measured twice in one week.
    """

    def __init__(self, grouping: Optional[EpisodeGrouping] = None) -> None:
        self._grouping = grouping if grouping is not None else EpisodeGrouping(BreakingEpisodeRule())
        # Per open episode key: what it is and how long it has been running. Kept so a process that
        # inherits state by replay can hand the display something to show (see `open_episodes`).
        self._open: Dict[str, OpenEpisode] = {}

    def get_rule(self) -> BreakingEpisodeRule:
        """The rule driving this tracker — surfaces read its gap to render live-vs-ended."""
        return self._grouping.rule

    def observe(self, envelope: AnalysisEnvelope) -> BreakingPass:
        """Fold one envelope into the episode state (edge-triggered).

        Every result is observed, not only the breaking ones: under hysteresis a pass below the
        confirm gate is what keeps an episode open, so skipping it would close stories early. This
        is also the seeding path — `pipeline_assembler` replays recent envelopes through it and
        discards the result, so live and store share one code path rather than two.
        """
        ts = envelope.timestamp
        outcome = BreakingPass()
        for result in envelope.result:
            # The episode key is the retrieval query — the analysis unit, not the ticker and not the
            # base currency (see `EpisodeGrouping.key_for`). One derivation, shared with the store
            # reports, so the live and batch views cannot group differently.
            group_key = self._grouping.key_for(result.symbol, result.base_currency)
            decision = self._grouping.rule.observe(group_key, ts, result.is_breaking, result.urgency)
            if decision.opened:
                engine, end_to_end = reaction_times(result, ts)
                episode = BreakingEpisode(result.symbol, result.signal, result.urgency,
                                          engine, end_to_end, len(result.sources),
                                          reason=result.reasoning)
                outcome.started.append(episode)
                self._open[group_key] = OpenEpisode(episode, started=ts, last_seen=ts)
            elif decision.held:
                outcome.held.append(result.symbol)
                running = self._open.get(group_key)
                if running is not None:
                    running.last_seen = ts
            if not decision.in_episode:
                # The gap closed it (or it never opened) — drop it so `open_episodes` only ever
                # reports what the rule still holds.
                self._open.pop(group_key, None)
        return outcome

    def seed(self, envelopes: List[AnalysisEnvelope]) -> List[OpenEpisode]:
        """Replay persisted envelopes to inherit episode state; returns what is still open.

        Drives the same `observe` the live path uses — seeding cannot drift from scoring — and
        then marks the episodes whose start sits at the window edge. That distinction is the whole
        reason this is a method rather than a loop at the call site: only here is it known which
        envelope was first, and therefore which starts are lower bounds.
        """
        for envelope in envelopes:
            self.observe(envelope)
        if envelopes:
            first_ts = envelopes[0].timestamp
            for running in self._open.values():
                running.started_bounded = running.started <= first_ts
        return list(self._open.values())

    def open_episodes(self) -> List[OpenEpisode]:
        """The episodes still running — for a fresh process to resume displaying them.

        Built from the decisions themselves rather than by reaching into the rule, so the two can
        never disagree about what is open.
        """
        return list(self._open.values())
