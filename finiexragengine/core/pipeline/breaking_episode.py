"""Edge-triggered breaking episodes (ISSUE_11 · groundwork for ISSUE_9).

A hot story stays `is_breaking` across many eval passes; counting or pushing on every pass inflates
"confirmed" and lets the reaction time grow with the wall-clock (it keeps re-anchoring on ageing
context articles). An **episode** is instead counted once, on the transition *into* breaking — the
streaming twin of the batch grouping the store-based `breaking_report` already does. `EPISODE_GAP`
lives here as the single source of truth both surfaces share, so the live dashboard and the store
report agree by construction.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from finiexragengine.types.outcome_types import AnalysisEnvelope, SentimentResult

# Consecutive is_breaking passes for one symbol within this gap = one episode; a longer gap starts
# a fresh one. Shared with reports/breaking_report.py so the live and store surfaces never diverge.
EPISODE_GAP = timedelta(minutes=30)


@dataclass
class BreakingEpisode:
    """One confirmed breaking episode — the start of a hot story, with its frozen reaction time."""
    symbol: str
    signal: str
    urgency: float
    engine_s: Optional[float]        # envelope ts − earliest source fetched_at (what we control)
    end_to_end_s: Optional[float]    # envelope ts − earliest REAL published_at (estimated excluded)
    n_sources: int
    # Why it broke (ISSUE_64 Phase 1): the LLM's own per-symbol `reasoning`, carried through so the
    # dashboard/report can show the trigger. Phase 2 replaces this with a dedicated `breaking_reason`.
    reason: str = ''


def reaction_times(result: SentimentResult, ts: datetime) -> Tuple[Optional[float], Optional[float]]:
    """`(engine_s, end_to_end_s)` for one breaking result — e2e ignores estimated publish dates.

    A date-less feed falls back to `published_at := fetched_at` (so recency filtering still works);
    those estimated dates would collapse e2e onto engine, so they are excluded from the e2e sample.
    None when no usable source timestamp exists (e2e then renders as `—`, honest).
    """
    fetched = [s.fetched_at for s in result.sources if s.fetched_at]
    published = [s.published_at for s in result.sources
                 if s.published_at and s.published_at != s.fetched_at]
    engine = (ts - min(fetched)).total_seconds() if fetched else None
    end_to_end = (ts - min(published)).total_seconds() if published else None
    return engine, end_to_end


class BreakingEpisodeTracker:
    """Streaming edge detector: which breaking results START a new episode this pass.

    In-memory / session-scoped (resets on restart) — the right lifetime for a live counter; the
    store report re-derives episodes from the persisted envelopes for the durable, restart-robust
    view. One tracker per eval worker (a worker owns one pipeline's symbols).
    """

    def __init__(self, gap: timedelta = EPISODE_GAP) -> None:
        self._gap = gap
        self._last: Dict[str, datetime] = {}     # symbol -> last is_breaking timestamp seen

    def new_episodes(self, envelope: AnalysisEnvelope) -> List[BreakingEpisode]:
        """The breaking results that transition into a NEW episode this pass (edge-triggered)."""
        ts = envelope.timestamp
        started: List[BreakingEpisode] = []
        for result in envelope.result:
            if not result.is_breaking:
                continue
            # Key the episode on the asset (base_currency), not the ticker: a query group's fanned
            # symbols (ETHUSD/ETHEUR, both base ETH) are one analysis → one episode, no double-count
            # (ISSUE_70). Falls back to the symbol for pre-#70 envelopes without a base.
            group_key = result.base_currency or result.symbol
            last = self._last.get(group_key)
            self._last[group_key] = ts           # every occurrence advances the gap anchor
            if last is not None and (ts - last) <= self._gap:
                continue                          # same ongoing story — not a new episode
            engine, end_to_end = reaction_times(result, ts)
            started.append(BreakingEpisode(result.symbol, result.signal, result.urgency,
                                           engine, end_to_end, len(result.sources),
                                           reason=result.reasoning))
        return started
