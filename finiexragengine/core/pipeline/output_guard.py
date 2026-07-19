"""Output consistency guard — deterministic post-validation of a scored row (ISSUE_35).

Schema validation (`SentimentLlmOutput`, Pydantic) proves an LLM completion is well-formed
and in range; it does not prove it is *coherent*. Valid-but-contradictory rows pass it:
`BUY` with a negative sentiment_score, a no-signal `HOLD` carrying near-certain confidence,
an empty reasoning. This guard is the semantic layer on top — pure code, zero API cost.
The `PipelineRunner` runs it after a successful evaluation, before envelope assembly, and
degrades a violated row to the contract HOLD (`envelope_contract.hold_result`) under a
`PARTIAL_RESPONSE` RunError; the guard itself only *detects*, it never mutates.

Deliberately deterministic: true semantic fidelity ("does the reasoning match the
articles?") would need an LLM judge — a second paid call per symbol, out of scope here.
If ever added, it must be *gated* to suspicious rows and fold into ISSUE_30's same-call
write-back, never a blanket second call.
"""
from dataclasses import dataclass
from typing import List

from finiexragengine.types.config_types.pipeline_config_types import OutputGuardConfig
from finiexragengine.types.outcome_types import SentimentResult


@dataclass(frozen=True)
class GuardViolation:
    """One violated coherence rule: `rule` names it, `detail` carries the offending values."""
    rule: str
    detail: str = ''

    def __str__(self) -> str:
        return f'{self.rule} ({self.detail})' if self.detail else self.rule


class OutputGuard:
    """Checks one scored row for internal coherence; returns the violated rules."""

    def __init__(self, config: OutputGuardConfig) -> None:
        self._config = config

    def violations(self, result: SentimentResult) -> List[GuardViolation]:
        """Violated rules for `result` — empty when the row is coherent.

        Judges only LLM-scored rows (`basis='llm'`): the mechanical no_data HOLD and
        already-degraded rows are engine-built, not model claims.
        """
        if result.basis != 'llm':
            return []
        found: List[GuardViolation] = []
        # Signal <-> score coherence: the categorical signal and the continuous score are
        # redundant claims about the same direction — a directional signal whose score sits
        # on the wrong side of zero (beyond the dead-zone tolerance) contradicts itself.
        tolerance = self._config.score_signal_tolerance
        if result.signal == 'BUY' and result.sentiment_score < -tolerance:
            found.append(GuardViolation(
                'signal_score_coherence', f'BUY with sentiment_score={result.sentiment_score}'))
        elif result.signal == 'SELL' and result.sentiment_score > tolerance:
            found.append(GuardViolation(
                'signal_score_coherence', f'SELL with sentiment_score={result.sentiment_score}'))
        # HOLD sanity: near-certainty attached to a no-signal HOLD reads as a degenerate
        # completion (fields filled mechanically), not a judgement.
        if result.signal == 'HOLD' and result.confidence > self._config.hold_confidence_max:
            found.append(GuardViolation(
                'hold_confidence', f'HOLD with confidence={result.confidence}'))
        # Non-empty reasoning: an unexplained signal is unusable downstream.
        if not result.reasoning.strip():
            found.append(GuardViolation('empty_reasoning'))
        # Provenance: a directional signal must cite sources. The engine attaches them
        # itself from the retrieved articles, so this is a cheap structural backstop.
        if result.signal != 'HOLD' and not result.sources:
            found.append(GuardViolation('missing_provenance', 'non-HOLD with no sources'))
        return found
