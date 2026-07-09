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
