"""BreakingEpisodeTracker (ISSUE_11) — edge-triggered breaking episodes, the live counterpart to
the store report's batch grouping: a hot story is counted once, not every pass it lingers.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from finiexragengine.core.pipeline.breaking_episode import (
    EPISODE_GAP,
    BreakingEpisodeTracker,
)
from finiexragengine.types.outcome_types import (
    ArticleRef,
    RunMetadata,
    SentimentEnvelope,
    SentimentResult,
)

_T0 = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _src(published: datetime, fetched: datetime) -> ArticleRef:
    return ArticleRef(article_id='a', url='u', title='t', published_at=published, fetched_at=fetched)


def _envelope(ts: datetime, *, symbol: str = 'ADAUSD', is_breaking: bool = True,
              sources: Optional[List[ArticleRef]] = None) -> SentimentEnvelope:
    result = SentimentResult(
        symbol=symbol, signal='SELL', sentiment_score=-0.5, confidence=0.8,
        reasoning='x', urgency=0.9, is_breaking=is_breaking, sources=sources or [])
    return SentimentEnvelope(
        pipeline_id='crypto_sentiment', outcome_type='sentiment_fear_greed', prompt_version='2',
        timestamp=ts, status='success', metadata=RunMetadata(model='m'), result=[result])


def test_first_breaking_is_one_episode():
    episodes = BreakingEpisodeTracker().new_episodes(_envelope(_T0))
    assert len(episodes) == 1 and episodes[0].symbol == 'ADAUSD'


def test_re_break_within_the_gap_is_not_a_new_episode():
    tracker = BreakingEpisodeTracker()
    tracker.new_episodes(_envelope(_T0))                            # episode start
    # 10 min later, still breaking — the same ongoing story, not counted again (the 248 bug).
    assert tracker.new_episodes(_envelope(_T0 + timedelta(minutes=10))) == []


def test_re_break_after_the_gap_starts_a_new_episode():
    tracker = BreakingEpisodeTracker()
    tracker.new_episodes(_envelope(_T0))
    tracker.new_episodes(_envelope(_T0 + timedelta(minutes=10)))    # within gap — no
    later = _T0 + timedelta(minutes=10) + EPISODE_GAP + timedelta(minutes=1)   # gap re-arms
    assert len(tracker.new_episodes(_envelope(later))) == 1


def test_ongoing_story_over_many_passes_counts_once():
    tracker = BreakingEpisodeTracker()
    total = sum(len(tracker.new_episodes(_envelope(_T0 + timedelta(minutes=10 * i))))
                for i in range(20))                                # 20 consecutive 10-min passes
    assert total == 1                                              # one episode, not twenty (was: 59/day)


def test_reaction_time_anchored_at_the_episode_start():
    src = _src(published=_T0 - timedelta(minutes=6), fetched=_T0 - timedelta(minutes=2))
    episode = BreakingEpisodeTracker().new_episodes(_envelope(_T0, sources=[src]))[0]
    assert round(episode.engine_s) == 120                          # t3 − fetched = 2 min
    assert round(episode.end_to_end_s) == 360                      # t3 − real published = 6 min


def test_estimated_publish_is_excluded_from_e2e():
    # published == fetched (a date-less feed's fallback) → estimated → dropped from e2e.
    est = _src(published=_T0 - timedelta(minutes=2), fetched=_T0 - timedelta(minutes=2))
    episode = BreakingEpisodeTracker().new_episodes(_envelope(_T0, sources=[est]))[0]
    assert round(episode.engine_s) == 120                          # engine still from fetched
    assert episode.end_to_end_s is None                            # no real published → honest '—'


def test_non_breaking_results_are_ignored():
    assert BreakingEpisodeTracker().new_episodes(_envelope(_T0, is_breaking=False)) == []


def test_reason_is_carried_from_reasoning():
    # ISSUE_64 Phase 1: the LLM's per-symbol reasoning rides along as the episode's `reason`.
    episode = BreakingEpisodeTracker().new_episodes(_envelope(_T0))[0]
    assert episode.reason == 'x'                                    # _envelope's reasoning
