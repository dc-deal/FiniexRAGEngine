"""Episode identity on the envelope (ISSUE_65) — what a consumer may rely on.

The field exists for one reason: a consumer gating on the raw `is_breaking` edge reacts 19-21 times
to a single story, because the LLM's `urgency` is quantised and drifts across the confirm gate
(measured, ISSUE_82). These tests are written against that promise — one story, one id — rather than
against the assignment code, so a later refactor of *where* the stamp happens cannot quietly break
*what* it guarantees.
"""
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from finiexragengine.core.pipeline.breaking_episode import BreakingEpisodeTracker
from finiexragengine.core.pipeline.breaking_episode_rule import (
    DEFAULT_EPISODE_GAP,
    BreakingEpisodeRule,
    EpisodeGrouping,
)
from finiexragengine.types.outcome_types import AnalysisEnvelope, RunMetadata, SentimentResult

_T0 = datetime(2026, 8, 24, 16, 51, 3, tzinfo=timezone.utc)


def _envelope(ts: datetime, rows: List[SentimentResult],
              pipeline_id: str = 'crypto_sentiment') -> AnalysisEnvelope:
    return AnalysisEnvelope(pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed',
                            prompt_version='3', timestamp=ts, status='success',
                            result=rows, metadata=RunMetadata(model='gpt-4o-mini'))


def _row(symbol: str, urgency: float, is_breaking: bool,
         base_currency: str = '') -> SentimentResult:
    return SentimentResult(symbol=symbol, signal='SELL', sentiment_score=-0.6, confidence=0.8,
                           reasoning='ECB signals an emergency review', urgency=urgency,
                           is_breaking=is_breaking, base_currency=base_currency or None)


@pytest.fixture
def tracker() -> BreakingEpisodeTracker:
    """One pipeline's tracker, both symbols on one retrieval query (the ISSUE_70 fan-out)."""
    return BreakingEpisodeTracker(EpisodeGrouping(
        BreakingEpisodeRule(), query_map={'ETHUSD': 'eth news', 'ETHEUR': 'eth news'}))


def test_one_story_keeps_one_id_across_every_pass(tracker: BreakingEpisodeTracker) -> None:
    """The promise itself: the id does not change while the episode runs."""
    ids = []
    for minutes, urgency, is_breaking in [(0, 0.9, True), (20, 0.7, False), (40, 0.9, True),
                                          (60, 0.3, False), (80, 0.7, False)]:
        envelope = _envelope(_T0 + timedelta(minutes=minutes), [_row('ETHUSD', urgency, is_breaking)])
        tracker.observe(envelope)
        ids.append(envelope.result[0].breaking_episode_id)

    assert len(set(ids)) == 1, f'the id flickered within one episode: {ids}'
    assert ids[0] == 'crypto_sentiment:eth news:2026-08-24T16:51:03Z'


def test_a_hold_band_pass_carries_the_id_although_it_is_not_breaking(
        tracker: BreakingEpisodeTracker) -> None:
    """An episode outlives its own boolean (ISSUE_82 hysteresis) — the id must too.

    This is the decision that deviates from the issue's original wording ('set only on breaking
    rows'), pinned here because reverting it silently would reintroduce exactly the flicker the
    field was added to remove.
    """
    tracker.observe(_envelope(_T0, [_row('ETHUSD', 0.9, True)]))
    held = _envelope(_T0 + timedelta(minutes=20), [_row('ETHUSD', 0.7, False)])
    tracker.observe(held)

    assert held.result[0].is_breaking is False
    assert held.result[0].breaking_episode_id is not None
    assert held.result[0].breaking_episode_start is False


def test_start_marks_the_opening_pass_only(tracker: BreakingEpisodeTracker) -> None:
    opening = _envelope(_T0, [_row('ETHUSD', 0.9, True)])
    later = _envelope(_T0 + timedelta(minutes=20), [_row('ETHUSD', 0.9, True)])
    tracker.observe(opening)
    tracker.observe(later)

    assert opening.result[0].breaking_episode_start is True
    assert later.result[0].breaking_episode_start is False   # still breaking, no longer an event


def test_a_row_outside_any_episode_carries_no_id(tracker: BreakingEpisodeTracker) -> None:
    quiet = _envelope(_T0, [_row('ETHUSD', 0.1, False)])
    tracker.observe(quiet)

    assert quiet.result[0].breaking_episode_id is None
    assert quiet.result[0].breaking_episode_start is False


def test_a_new_story_after_the_gap_gets_a_new_id(tracker: BreakingEpisodeTracker) -> None:
    first = _envelope(_T0, [_row('ETHUSD', 0.9, True)])
    tracker.observe(first)
    after_gap = _envelope(_T0 + DEFAULT_EPISODE_GAP + timedelta(minutes=1),
                          [_row('ETHUSD', 0.9, True)])
    tracker.observe(after_gap)

    assert after_gap.result[0].breaking_episode_id != first.result[0].breaking_episode_id
    assert after_gap.result[0].breaking_episode_start is True


def test_one_analysis_fanned_to_two_symbols_is_one_episode(tracker: BreakingEpisodeTracker) -> None:
    """ETHUSD and ETHEUR are one retrieval query (ISSUE_70) — one story, one id, one registry row."""
    envelope = _envelope(_T0, [_row('ETHUSD', 0.9, True), _row('ETHEUR', 0.9, True)])
    outcome = tracker.observe(envelope)

    assert envelope.result[0].breaking_episode_id == envelope.result[1].breaking_episode_id
    # One row per episode per pass, not one per symbol: `n_passes` counts passes, and a fanned
    # analysis would otherwise count one pass twice.
    assert len(outcome.episodes) == 1


def test_symbols_sharing_only_a_base_currency_do_not_share_an_episode() -> None:
    """The FX collision ISSUE_82 fixed: USDJPY and USDCAD are separate analyses."""
    separate = BreakingEpisodeTracker(EpisodeGrouping(
        BreakingEpisodeRule(), query_map={'USDJPY': 'jpy news', 'USDCAD': 'cad news'}))
    envelope = _envelope(_T0, [_row('USDJPY', 0.9, True, base_currency='USD'),
                               _row('USDCAD', 0.9, True, base_currency='USD')])
    separate.observe(envelope)

    assert envelope.result[0].breaking_episode_id != envelope.result[1].breaking_episode_id


def test_the_id_survives_a_restart_that_clips_the_episode_start() -> None:
    """A fresh process must rejoin the running story, not mint a second identity for it.

    The seed window is finite and the hold band can keep an episode alive far past its last
    breaking pass (measured tails of 5 h, 8.7 h and 33 h, ISSUE_82). So the replay may start
    *inside* an episode: the rule then opens it at the first breaking pass it sees, whose start is
    later than the real one. Minting from that clipped start would hand the consumer a second id
    for a story it is already tracking — the replayed envelopes carry the original, so it is
    adopted instead.
    """
    grouping = EpisodeGrouping(BreakingEpisodeRule(), query_map={'BTCUSD': 'btc news'})
    live = [_envelope(_T0, [_row('BTCUSD', 0.9, True)]),
            _envelope(_T0 + timedelta(minutes=30), [_row('BTCUSD', 0.7, False)]),
            _envelope(_T0 + timedelta(minutes=60), [_row('BTCUSD', 0.9, True)])]   # a re-trigger
    before = BreakingEpisodeTracker(grouping)
    for envelope in live:
        before.observe(envelope)
    original = live[0].result[0].breaking_episode_id

    # A new process whose seed window reaches back only as far as the re-trigger.
    after_restart = BreakingEpisodeTracker(EpisodeGrouping(
        BreakingEpisodeRule(), query_map={'BTCUSD': 'btc news'}))
    after_restart.seed(live[2:])
    next_pass = _envelope(_T0 + timedelta(minutes=90), [_row('BTCUSD', 0.7, False)])
    after_restart.observe(next_pass)

    assert next_pass.result[0].breaking_episode_id == original


def test_an_archived_envelope_without_the_fields_still_parses() -> None:
    """Additive with defaults: the archive predates ISSUE_65 and must keep loading."""
    legacy = {'symbol': 'BTCUSD', 'signal': 'HOLD', 'sentiment_score': 0.0, 'confidence': 0.0,
              'reasoning': 'No relevant news found'}
    row = SentimentResult.model_validate(legacy)

    assert row.breaking_episode_id is None
    assert row.breaking_episode_start is False


def test_the_registry_row_freezes_the_opening_pass_and_advances_the_rest(
        tracker: BreakingEpisodeTracker) -> None:
    """What the rows say: the opener carries the reaction measurement, continuations do not."""
    opened = tracker.observe(_envelope(_T0, [_row('ETHUSD', 0.9, True)])).episodes[0]
    continued = tracker.observe(
        _envelope(_T0 + timedelta(minutes=20), [_row('ETHUSD', 0.7, False)])).episodes[0]

    assert opened.opened is True and continued.opened is False
    assert opened.episode_id == continued.episode_id
    assert continued.started_at == opened.started_at        # identity is anchored on the start
    assert continued.last_seen_at > opened.last_seen_at     # only this advances
    assert continued.engine_s is None                       # never re-sampled (ISSUE_81)
    assert opened.prompt_version == '3'                     # scores compare only within one
