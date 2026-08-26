"""Tests for the OutputGuard (ISSUE_35) — deterministic coherence rules, no DB/API.

The guard is pure code over one SentimentResult: coherent rows pass untouched, each
contradiction names its rule. Degradation itself (HOLD row, PARTIAL_RESPONSE, partial
status) is the runner's job and is tested in test_pipeline_runner.py.
"""
from datetime import datetime, timezone
from typing import List, Optional

from finiexragengine.core.pipeline.output_guard import GuardViolation, OutputGuard
from finiexragengine.types.config_types.pipeline_config_types import OutputGuardConfig
from finiexragengine.types.outcome_types import ArticleRef, SentimentResult

_TS = datetime(2026, 7, 1, tzinfo=timezone.utc)
_REF = ArticleRef(article_id='a1', url='https://example.test/a1', title='t',
                  published_at=_TS, fetched_at=_TS)


def _row(signal: str = 'BUY', score: float = 0.5, confidence: float = 0.8,
         reasoning: str = 'bullish flow', sources: Optional[List[ArticleRef]] = None,
         basis: str = 'llm') -> SentimentResult:
    # Sources default to one real ref for directional signals — the shape the evaluator
    # emits; tests drop them explicitly to trigger the provenance rule.
    if sources is None:
        sources = [] if signal == 'HOLD' else [_REF]
    return SentimentResult(symbol='BTCUSD', signal=signal, sentiment_score=score,
                           confidence=confidence, reasoning=reasoning,
                           sources=sources, basis=basis)


def _guard(**overrides) -> OutputGuard:
    return OutputGuard(OutputGuardConfig(**overrides))


def _rules(violations: List[GuardViolation]) -> List[str]:
    return [violation.rule for violation in violations]


# --- coherent rows pass ---

def test_coherent_rows_pass():
    guard = _guard()
    assert guard.violations(_row('BUY', score=0.7)) == []
    assert guard.violations(_row('SELL', score=-0.6)) == []
    assert guard.violations(_row('HOLD', score=0.0, confidence=0.4)) == []


def test_wobble_inside_the_dead_zone_passes():
    # A directional signal may sit slightly on the wrong side of zero — nuance, not
    # contradiction. The boundary itself (== -tolerance) still passes; only beyond fires.
    guard = _guard()
    assert guard.violations(_row('BUY', score=-0.05)) == []
    assert guard.violations(_row('BUY', score=-0.1)) == []
    assert guard.violations(_row('SELL', score=0.1)) == []


# --- each contradiction fires its rule ---

def test_buy_with_negative_score_fires():
    violations = _guard().violations(_row('BUY', score=-0.7))
    assert _rules(violations) == ['signal_score_coherence']
    assert 'BUY with sentiment_score=-0.7' in str(violations[0])


def test_sell_with_positive_score_fires():
    violations = _guard().violations(_row('SELL', score=0.7))
    assert _rules(violations) == ['signal_score_coherence']


def test_hold_with_near_certain_confidence_fires():
    violations = _guard().violations(_row('HOLD', score=0.0, confidence=0.99))
    assert _rules(violations) == ['hold_confidence']
    # The boundary itself passes — strict 'above' fires, so 1.0 can disable the rule.
    assert _guard().violations(_row('HOLD', score=0.0, confidence=0.9)) == []


def test_empty_reasoning_fires():
    assert _rules(_guard().violations(_row(reasoning=''))) == ['empty_reasoning']
    assert _rules(_guard().violations(_row(reasoning='   '))) == ['empty_reasoning']


def test_directional_signal_without_sources_fires():
    violations = _guard().violations(_row('BUY', score=0.5, sources=[]))
    assert _rules(violations) == ['missing_provenance']
    # A HOLD legitimately carries no sources — no provenance demanded.
    assert _guard().violations(_row('HOLD', score=0.0, confidence=0.5, sources=[])) == []


def test_multiple_violations_accumulate():
    violations = _guard().violations(_row('BUY', score=-0.9, reasoning='', sources=[]))
    assert _rules(violations) == ['signal_score_coherence', 'empty_reasoning',
                                  'missing_provenance']


# --- knobs and skips ---

def test_tolerance_knob_widens_the_dead_zone():
    assert _guard(score_signal_tolerance=0.5).violations(_row('BUY', score=-0.4)) == []


def test_hold_confidence_knob_can_disable_the_rule():
    assert _guard(hold_confidence_max=1.0).violations(
        _row('HOLD', score=0.0, confidence=1.0)) == []


def test_non_llm_rows_are_never_judged():
    # The mechanical no_data HOLD and already-degraded rows are engine-built, not model
    # claims — even a shape that would violate every rule is skipped.
    weird = _row('BUY', score=-0.9, reasoning='', sources=[], basis='no_data')
    assert _guard().violations(weird) == []


def test_violation_renders_rule_and_detail():
    assert str(GuardViolation('empty_reasoning')) == 'empty_reasoning'
    assert str(GuardViolation('hold_confidence', 'HOLD with confidence=0.99')) == (
        'hold_confidence (HOLD with confidence=0.99)')
