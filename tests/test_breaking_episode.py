"""BreakingEpisodeTracker (ISSUE_11 · ISSUE_82) — the live driver of the episode rule.

A hot story is counted once, not every pass it lingers, and — since ISSUE_82 — a single pass
dipping below the confirm gate no longer ends it.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from finiexragengine.core.pipeline.breaking_episode import BreakingEpisodeTracker
from finiexragengine.core.pipeline.breaking_episode_rule import (
    DEFAULT_EPISODE_GAP,
    BreakingEpisodeRule,
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
              urgency: float = 0.9,
              sources: Optional[List[ArticleRef]] = None) -> SentimentEnvelope:
    result = SentimentResult(
        symbol=symbol, signal='SELL', sentiment_score=-0.5, confidence=0.8,
        reasoning='x', urgency=urgency, is_breaking=is_breaking, sources=sources or [])
    return SentimentEnvelope(
        pipeline_id='crypto_sentiment', outcome_type='sentiment_fear_greed', prompt_version='2',
        timestamp=ts, status='success', metadata=RunMetadata(model='m'), result=[result])


def test_first_breaking_is_one_episode():
    started = BreakingEpisodeTracker().observe(_envelope(_T0)).started
    assert len(started) == 1 and started[0].symbol == 'ADAUSD'


def test_re_break_within_the_gap_is_not_a_new_episode():
    tracker = BreakingEpisodeTracker()
    tracker.observe(_envelope(_T0))                                 # episode start
    # 10 min later, still breaking — the same ongoing story, not counted again (the 248 bug).
    outcome = tracker.observe(_envelope(_T0 + timedelta(minutes=10)))
    assert outcome.started == [] and outcome.held == ['ADAUSD']


def test_re_break_after_the_gap_starts_a_new_episode():
    tracker = BreakingEpisodeTracker()
    tracker.observe(_envelope(_T0))
    tracker.observe(_envelope(_T0 + timedelta(minutes=10)))         # within gap — no
    later = _T0 + timedelta(minutes=10) + DEFAULT_EPISODE_GAP + timedelta(minutes=1)
    assert len(tracker.observe(_envelope(later)).started) == 1


def test_ongoing_story_over_many_passes_counts_once():
    tracker = BreakingEpisodeTracker()
    total = sum(len(tracker.observe(_envelope(_T0 + timedelta(minutes=10 * i))).started)
                for i in range(20))                                # 20 consecutive 10-min passes
    assert total == 1                                              # one episode, not twenty


def test_reaction_time_anchored_at_the_episode_start():
    src = _src(published=_T0 - timedelta(minutes=6), fetched=_T0 - timedelta(minutes=2))
    episode = BreakingEpisodeTracker().observe(_envelope(_T0, sources=[src])).started[0]
    assert round(episode.engine_s) == 120                          # t3 − fetched = 2 min
    assert round(episode.end_to_end_s) == 360                      # t3 − real published = 6 min


def test_estimated_publish_is_excluded_from_e2e():
    # published == fetched (a date-less feed's fallback) → estimated → dropped from e2e.
    est = _src(published=_T0 - timedelta(minutes=2), fetched=_T0 - timedelta(minutes=2))
    episode = BreakingEpisodeTracker().observe(_envelope(_T0, sources=[est])).started[0]
    assert round(episode.engine_s) == 120                          # engine still from fetched
    assert episode.end_to_end_s is None                            # no real published → honest '—'


def test_a_non_breaking_pass_opens_nothing():
    outcome = BreakingEpisodeTracker().observe(
        _envelope(_T0, is_breaking=False, urgency=0.1))
    assert outcome.started == [] and outcome.held == []


def test_reason_is_carried_from_reasoning():
    # ISSUE_64 Phase 1: the LLM's per-symbol reasoning rides along as the episode's `reason`.
    episode = BreakingEpisodeTracker().observe(_envelope(_T0)).started[0]
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
    outcome = BreakingEpisodeTracker().observe(env)
    assert len(outcome.started) == 1 and outcome.started[0].symbol == 'ETHUSD'
    assert outcome.held == ['ETHEUR']          # the fanned twin holds the same asset's episode


# --- ISSUE_82: hysteresis — one dip must not end a story -------------------------------------


def test_a_dip_below_the_confirm_gate_holds_the_episode():
    """The production defect, as a regression.

    `urgency` is quantised to seven values and the confirm gate sits on one of them, so a story
    oscillated 0.8/0.7 on a byte-identical source set. Under the old rule the 0.7 passes counted
    as "not breaking" and two of them ended the episode; here they hold it open.
    """
    tracker = BreakingEpisodeTracker()
    assert len(tracker.observe(_envelope(_T0, urgency=0.8)).started) == 1
    for minute in (10, 20, 30):                                    # three passes at the exit gate
        outcome = tracker.observe(_envelope(_T0 + timedelta(minutes=minute),
                                            is_breaking=False, urgency=0.7))
        assert outcome.started == [] and outcome.held == ['ADAUSD']
    # Back above the confirm gate 40 minutes in — still the SAME story, not a second episode.
    assert tracker.observe(_envelope(_T0 + timedelta(minutes=40), urgency=0.8)).started == []


def test_a_drop_below_the_exit_gate_closes_after_the_gap():
    tracker = BreakingEpisodeTracker()
    tracker.observe(_envelope(_T0, urgency=0.8))
    quiet = _envelope(_T0 + timedelta(minutes=10), is_breaking=False, urgency=0.3)
    assert tracker.observe(quiet).held == []                       # below exit → nothing held
    # Past the gap measured from the last qualifying pass, a fresh confirm opens a new episode.
    later = _T0 + DEFAULT_EPISODE_GAP + timedelta(minutes=1)
    assert len(tracker.observe(_envelope(later, urgency=0.8)).started) == 1


def test_a_dip_below_the_exit_gate_inside_the_gap_is_still_one_episode():
    tracker = BreakingEpisodeTracker()
    tracker.observe(_envelope(_T0, urgency=0.8))
    tracker.observe(_envelope(_T0 + timedelta(minutes=10), is_breaking=False, urgency=0.6))
    # 20 minutes total — well inside the gap, so the story never closed.
    assert tracker.observe(_envelope(_T0 + timedelta(minutes=20), urgency=0.8)).started == []


def test_opening_uses_the_recorded_verdict_not_todays_threshold():
    """An archived pass keeps the verdict its pipeline actually took.

    A high `urgency` with `is_breaking=False` is what a pass looks like when it was scored under a
    stricter threshold. Re-deriving the open decision from urgency would rewrite that history on
    every report run; the rule must not.
    """
    outcome = BreakingEpisodeTracker().observe(
        _envelope(_T0, is_breaking=False, urgency=0.95))
    assert outcome.started == []


def test_the_measured_xrpusd_sequence_is_one_episode():
    """2026-08-17, the case that produced ISSUE_82 — real urgencies, real cadence.

    Fifteen passes on a byte-identical source set; the old rule split them into two episodes
    because 13:40–14:00 sat below the confirm gate for 40 minutes.
    """
    measured = [0.8, 0.8, 0.8, 0.8, 0.7, 0.7, 0.7, 0.8, 0.7, 0.8, 0.7, 0.8, 0.6, 0.8]
    tracker = BreakingEpisodeTracker()
    opened = 0
    for index, urgency in enumerate(measured):
        outcome = tracker.observe(_envelope(_T0 + timedelta(minutes=10 * index),
                                            symbol='XRPUSD', is_breaking=urgency >= 0.8,
                                            urgency=urgency))
        opened += len(outcome.started)
    assert opened == 1                                             # was 2 before ISSUE_82


def test_the_rule_is_configurable_per_pipeline():
    # Setting the exit gate equal to the confirm gate disables the hysteresis — the documented
    # way back to the pre-ISSUE_82 behaviour.
    rule = BreakingEpisodeRule(exit_threshold=0.8, gap=timedelta(minutes=30))
    tracker = BreakingEpisodeTracker(rule)
    tracker.observe(_envelope(_T0, urgency=0.8))
    tracker.observe(_envelope(_T0 + timedelta(minutes=10), is_breaking=False, urgency=0.7))
    assert len(tracker.observe(_envelope(_T0 + timedelta(minutes=40), urgency=0.8)).started) == 1


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

    episode = BreakingEpisodeTracker().observe(
        _envelope(_T0, sources=[stale_context, triggering])).started[0]

    assert round(episode.engine_s) == 30            # the freshest evidence, not the oldest
    assert round(episode.end_to_end_s) == 45
    assert episode.engine_s < 60, 'anchoring on the oldest source reports ~20h here'


def test_source_order_does_not_change_the_anchor():
    # Retrieval order is by similarity, not by time — the metric must not depend on it.
    old = _src(published=_T0 - timedelta(hours=6), fetched=_T0 - timedelta(hours=6))
    fresh = _src(published=_T0 - timedelta(minutes=2), fetched=_T0 - timedelta(minutes=1))
    forwards = BreakingEpisodeTracker().observe(_envelope(_T0, sources=[old, fresh])).started[0]
    backwards = BreakingEpisodeTracker().observe(_envelope(_T0, sources=[fresh, old])).started[0]
    assert forwards.engine_s == backwards.engine_s == 60


def test_a_single_source_is_unaffected_by_the_change():
    # The one case where min and max agree — kept so the fix cannot silently break the simple path.
    only = _src(published=_T0 - timedelta(minutes=5), fetched=_T0 - timedelta(minutes=3))
    episode = BreakingEpisodeTracker().observe(_envelope(_T0, sources=[only])).started[0]
    assert round(episode.engine_s) == 180 and round(episode.end_to_end_s) == 300
