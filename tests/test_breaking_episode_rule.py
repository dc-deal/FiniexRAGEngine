"""BreakingEpisodeRule (ISSUE_82) — the Schmitt trigger both breaking surfaces drive.

The tracker and the store report have their own suites; this one pins the decision itself, so a
change in semantics fails here first rather than as a surprise in two report assertions.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.pipeline.breaking_episode_rule import (
    DEFAULT_EPISODE_GAP,
    DEFAULT_EXIT_THRESHOLD,
    BreakingEpisodeRule,
    EpisodeGrouping,
    grouping_from_config,
    groupings_from_configs,
)
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig

_T0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


_FX_SYMBOLS = [
    {'key': 'USDJPY', 'base': 'USD', 'quote': 'JPY', 'query': 'US Dollar Japanese Yen'},
    {'key': 'USDCAD', 'base': 'USD', 'quote': 'CAD', 'query': 'US Dollar Canadian Dollar'},
    {'key': 'ETHUSD', 'base': 'ETH', 'quote': 'USD', 'query': 'Ethereum ETH'},
    {'key': 'ETHEUR', 'base': 'ETH', 'quote': 'EUR', 'query': 'Ethereum ETH'},
]


def _config(pipeline_id: str, *, symbols: object = None, **breaking: object) -> PipelineConfig:
    return PipelineConfig(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed', market='crypto',
        symbols=symbols or [{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD',
                             'query': 'Bitcoin BTC'}],
        prompt={'name': 'crypto_sentiment', 'version': '2'}, llm={'model': 'gpt-4o-mini'},
        trigger={'type': 'interval', 'timeframe': 'M10'}, source_set='crypto_news',
        breaking=breaking or {})


# --- the three flags -------------------------------------------------------------------------

def test_a_confirmed_pass_opens_and_reports_its_start():
    decision = BreakingEpisodeRule().observe('BTC', _T0, True, 0.8)
    assert decision.opened and decision.in_episode and not decision.held
    assert decision.started_at == _T0


def test_nothing_opens_below_the_confirm_gate():
    rule = BreakingEpisodeRule()
    decision = rule.observe('BTC', _T0, False, DEFAULT_EXIT_THRESHOLD)
    assert not decision.opened and not decision.in_episode and not decision.held
    assert decision.started_at is None
    assert not rule.is_open('BTC'), 'the exit gate holds an episode, it must never open one'


def test_a_hold_keeps_the_original_start():
    rule = BreakingEpisodeRule()
    rule.observe('BTC', _T0, True, 0.8)
    decision = rule.observe('BTC', _at(10), False, DEFAULT_EXIT_THRESHOLD)
    assert decision.held and decision.in_episode and not decision.opened
    assert decision.started_at == _T0, 'the episode start is frozen, not re-stamped per pass'


def test_a_dip_below_the_exit_gate_stays_in_the_episode_until_the_gap():
    """The distinction `in_episode` exists for: the story is not over, but its clock is running."""
    rule = BreakingEpisodeRule()
    rule.observe('BTC', _T0, True, 0.8)
    dip = rule.observe('BTC', _at(10), False, 0.3)
    assert dip.in_episode and not dip.held and not dip.opened
    assert rule.is_open('BTC')


# --- the gap ----------------------------------------------------------------------------------

def test_the_gap_is_measured_from_the_last_qualifying_pass_not_the_last_pass():
    """A run of dips must not keep an episode alive by merely existing.

    This is the trap in a gap rule driven per pass: if any pass reset the clock, a symbol
    evaluated every ten minutes would never close an episode at all.
    """
    rule = BreakingEpisodeRule(gap=timedelta(minutes=45))
    rule.observe('BTC', _T0, True, 0.8)
    for minute in range(10, 60, 10):                     # 10..50, all below the exit gate
        rule.observe('BTC', _at(minute), False, 0.2)
    assert not rule.is_open('BTC'), 'dips reset nothing; the gap ran out 45 min after the open'


def test_a_pass_exactly_on_the_gap_boundary_still_belongs_to_the_episode():
    # `>` not `>=`: the boundary is inclusive, so a cadence that lands exactly on the gap does not
    # flip on rounding. The 30-minute default did exactly that on a 600s grid (ISSUE_82).
    rule = BreakingEpisodeRule(gap=timedelta(minutes=45))
    rule.observe('BTC', _T0, True, 0.8)
    assert not rule.observe('BTC', _at(45), True, 0.8).opened


def test_past_the_gap_a_confirmed_pass_opens_a_new_episode():
    rule = BreakingEpisodeRule(gap=timedelta(minutes=45))
    rule.observe('BTC', _T0, True, 0.8)
    decision = rule.observe('BTC', _at(46), True, 0.8)
    assert decision.opened and decision.started_at == _at(46)


# --- keys are independent ----------------------------------------------------------------------

def test_keys_do_not_share_state():
    rule = BreakingEpisodeRule()
    rule.observe('BTC', _T0, True, 0.8)
    assert rule.observe('ETH', _at(10), False, 0.9).in_episode is False
    assert rule.is_open('BTC') and not rule.is_open('ETH')


# --- config constructors ------------------------------------------------------------------------

def test_grouping_from_config_reads_the_breaking_block():
    grouping = grouping_from_config(
        _config('p', urgency_exit_threshold=0.55, episode_gap_minutes=90))
    assert grouping.rule.get_exit_threshold() == 0.55
    assert grouping.rule.get_gap() == timedelta(minutes=90)


# --- the episode key (ISSUE_82 finding 6) ------------------------------------------------------

def test_same_query_symbols_share_one_episode_key():
    # ISSUE_70: ETHUSD and ETHEUR are one analysis fanned to two labels, so one episode.
    grouping = grouping_from_config(_config('p', symbols=_FX_SYMBOLS))
    assert grouping.key_for('ETHUSD', 'ETH') == grouping.key_for('ETHEUR', 'ETH')


def test_same_base_different_query_symbols_do_not():
    """The production defect, as a regression.

    USDJPY/USDCAD/USDCHF share the base USD but are three separate retrieval queries. Keying on the
    base put them in one episode: on 2026-08-18 only USDCAD broke, yet USDJPY — in the hold band
    half the time — kept re-anchoring the gap on an episode it had no part in.
    """
    grouping = grouping_from_config(_config('p', symbols=_FX_SYMBOLS))
    assert grouping.key_for('USDJPY', 'USD') != grouping.key_for('USDCAD', 'USD')


def test_an_unconfigured_symbol_falls_back_to_its_base():
    # A retired or renamed stream still sits in the archive; it must key as it did before the fix
    # rather than splitting off a phantom unit.
    grouping = grouping_from_config(_config('p', symbols=_FX_SYMBOLS))
    assert grouping.key_for('DOGEUSD', 'DOGE') == 'DOGE'
    assert grouping.key_for('DOGEUSD', None) == 'DOGEUSD'


def test_a_grouping_without_a_map_keys_on_the_base():
    # The no-config-context default — what a caller that was handed no registry should get.
    grouping = EpisodeGrouping(BreakingEpisodeRule())
    assert grouping.key_for('USDJPY', 'USD') == grouping.key_for('USDCAD', 'USD') == 'USD'


def test_defaults_match_the_schema():
    # The bare rule (tests, legacy call sites) must not disagree with what the engine runs on.
    schema = _config('p').breaking
    assert BreakingEpisodeRule().get_exit_threshold() == schema.urgency_exit_threshold
    assert BreakingEpisodeRule().get_gap() == timedelta(minutes=schema.episode_gap_minutes)
    assert DEFAULT_EPISODE_GAP == timedelta(minutes=schema.episode_gap_minutes)
    assert DEFAULT_EXIT_THRESHOLD == schema.urgency_exit_threshold


def test_groupings_from_configs_keys_by_pipeline_id():
    groupings = groupings_from_configs([_config('crypto_sentiment', episode_gap_minutes=45),
                                        _config('forex_macro_sentiment', episode_gap_minutes=90)])
    assert set(groupings) == {'crypto_sentiment', 'forex_macro_sentiment'}
    assert groupings['forex_macro_sentiment'].rule.get_gap() == timedelta(minutes=90)
    # Rule and key travel together: a grouping always carries its pipeline's symbol table.
    assert groupings['crypto_sentiment'].key_for('BTCUSD', 'BTC') == 'Bitcoin BTC'


def test_disabling_the_hysteresis_restores_the_pre_issue_82_behaviour():
    # Documented escape hatch: exit == confirm means only a breaking pass holds a story open.
    rule = BreakingEpisodeRule(exit_threshold=0.8, gap=timedelta(minutes=30))
    rule.observe('BTC', _T0, True, 0.8)
    rule.observe('BTC', _at(10), False, 0.7)            # would hold with hysteresis; does not here
    assert rule.observe('BTC', _at(31), True, 0.8).opened
