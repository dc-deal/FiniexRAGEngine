"""SentimentLlmOutput validation (ISSUE_6) — the LLM-scored subset must be strict."""
import pytest
from pydantic import ValidationError

from finiexragengine.types.outcome_types import SentimentLlmOutput


def test_valid_output():
    out = SentimentLlmOutput(signal='BUY', sentiment_score=0.5, confidence=0.9,
                             reasoning='ETF inflows accelerate', urgency=0.3)
    assert out.signal == 'BUY'


def test_rejects_unknown_signal():
    with pytest.raises(ValidationError):
        SentimentLlmOutput(signal='MAYBE', sentiment_score=0.0, confidence=0.5,
                           reasoning='x', urgency=0.1)


def test_rejects_out_of_range_score():
    with pytest.raises(ValidationError):
        SentimentLlmOutput(signal='HOLD', sentiment_score=2.0, confidence=0.5,
                           reasoning='x', urgency=0.1)


def test_forbids_extra_fields():
    # The LLM must not invent fields (e.g. provenance) — the engine attaches those.
    with pytest.raises(ValidationError):
        SentimentLlmOutput(signal='HOLD', sentiment_score=0.0, confidence=0.5,
                           reasoning='x', urgency=0.1, sources=['made-up'])


def test_breaking_reason_is_optional():
    # The model returns it only when something is actually breaking (ISSUE_64 Phase 2, prompt v3).
    # A required field would force it to invent a shock on every quiet pass.
    quiet = SentimentLlmOutput(signal='HOLD', sentiment_score=0.0, confidence=0.4,
                               reasoning='Mixed coverage, no clear driver', urgency=0.2)
    assert quiet.breaking_reason is None


def test_breaking_reason_is_accepted_when_present():
    out = SentimentLlmOutput(signal='SELL', sentiment_score=-0.7, confidence=0.9,
                             reasoning='Regulatory pressure dominates the coverage', urgency=0.9,
                             breaking_reason='SEC sues Bitmine over ETH treasury buys; '
                                             'desks flipping risk-off')
    assert out.breaking_reason.startswith('SEC sues Bitmine')
