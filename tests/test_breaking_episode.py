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


def test_fanned_same_base_symbols_are_one_episode():
    # ISSUE_70 Schicht 2: ETHUSD + ETHEUR (both base ETH, fanned from one analysis) collapse to ONE
    # episode, not two — keyed on the asset, so the confirmed count is not doubled.
    def _r(symbol: str) -> SentimentResult:
        return SentimentResult(symbol=symbol, signal='SELL', sentiment_score=-0.5, confidence=0.8,
                               reasoning='hack', urgency=0.9, is_breaking=True, base_currency='ETH')
    env = SentimentEnvelope(
        pipeline_id='crypto_sentiment', outcome_type='sentiment_fear_greed', prompt_version='2',
        timestamp=_T0, status='success', metadata=RunMetadata(model='m'),
        result=[_r('ETHUSD'), _r('ETHEUR')])
    episodes = BreakingEpisodeTracker().new_episodes(env)
    assert len(episodes) == 1 and episodes[0].symbol == 'ETHUSD'    # one asset-level episode


# --- ISSUE_81: the anchor is the freshest source, not the oldest ----------------------------


def test_reaction_is_not_measured_from_the_oldest_context_article():
    """The production defect, as a regression.

    A pass retrieves context up to `recency_window_minutes` back (1440 = 24h). Anchoring on the
    OLDEST of those made the reported reaction time track the retrieval window, not the engine:
    production showed ~21h for a pipeline that evaluates every 10 minutes and jumps the queue on
    a breaking wake in seconds. Here the triggering article is 30s old and a stale context article
    is 20h old — the number must follow the fresh one.
    """
    triggering = _src(published=_T0 - timedelta(seconds=45), fetched=_T0 - timedelta(seconds=30))
    stale_context = _src(published=_T0 - timedelta(hours=20), fetched=_T0 - timedelta(hours=20))

    episode = BreakingEpisodeTracker().new_episodes(
        _envelope(_T0, sources=[stale_context, triggering]))[0]

    assert round(episode.engine_s) == 30            # the freshest evidence, not the oldest
    assert round(episode.end_to_end_s) == 45
    assert episode.engine_s < 60, 'anchoring on the oldest source reports ~20h here'


def test_source_order_does_not_change_the_anchor():
    # Retrieval order is by similarity, not by time — the metric must not depend on it.
    old = _src(published=_T0 - timedelta(hours=6), fetched=_T0 - timedelta(hours=6))
    fresh = _src(published=_T0 - timedelta(minutes=2), fetched=_T0 - timedelta(minutes=1))
    forwards = BreakingEpisodeTracker().new_episodes(_envelope(_T0, sources=[old, fresh]))[0]
    backwards = BreakingEpisodeTracker().new_episodes(_envelope(_T0, sources=[fresh, old]))[0]
    assert forwards.engine_s == backwards.engine_s == 60


def test_a_single_source_is_unaffected_by_the_change():
    # The one case where min and max agree — kept so the fix cannot silently break the simple path.
    only = _src(published=_T0 - timedelta(minutes=5), fetched=_T0 - timedelta(minutes=3))
    episode = BreakingEpisodeTracker().new_episodes(_envelope(_T0, sources=[only]))[0]
    assert round(episode.engine_s) == 180 and round(episode.end_to_end_s) == 300
