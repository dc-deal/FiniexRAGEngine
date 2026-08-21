"""Pipeline — the registry-facing handle for one constellation."""
from datetime import datetime, timezone
from typing import List, Optional

from finiexragengine.core.pipeline.pipeline_runner import PipelineRunner
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.outcome_types import (
    AnalysisEnvelope,
    RunMetadata,
    SentimentResult,
)
from finiexragengine.types.trigger_types import TriggerReason


class Pipeline:
    """Holds one constellation's config and runs it through its attached runner.

    Construction is two-phase: the registry creates the handle from the validated
    config (no I/O); the `PipelineAssembler` later attaches a `PipelineRunner` — the
    real staged flow (ISSUE_7) — once DB/API wiring is available. Without a runner,
    `run()` falls back to the deterministic scaffold mock, so the service (and the
    free test suite) still boots with no database or API key configured.
    """

    def __init__(self, config: PipelineConfig) -> None:
        self._config = config
        self._runner: Optional[PipelineRunner] = None    # set via set_runner

    def get_config(self) -> PipelineConfig:
        return self._config

    def set_runner(self, runner: PipelineRunner) -> None:
        """Attach the real staged runner (built by the PipelineAssembler, ISSUE_7)."""
        self._runner = runner

    def run(self, reason: TriggerReason) -> AnalysisEnvelope:
        """Execute the pipeline once and return its outcome envelope.

        `reason` says why this pass runs (ISSUE_87) and is stamped on the envelope. Required, not
        defaulted: every caller knows its own reason, and a default would quietly reintroduce the
        blind spot — a scheduled tick, a restart and a breaking wake looking identical downstream.
        """
        if self._runner is not None:
            return self._runner.run(reason)
        return self._mock_envelope(reason)

    def _mock_envelope(self, reason: TriggerReason) -> AnalysisEnvelope:
        """Scaffold fallback: a valid, deterministic envelope without any wiring.

        Envelope invariant: every requested symbol is always present in the result —
        even the mock emits one HOLD/0.0 entry per configured symbol, never a gap.
        No prompt fingerprint is stamped — these results never ran a prompt (ISSUE_33).
        """
        now = datetime.now(timezone.utc)
        results: List[SentimentResult] = [
            SentimentResult(
                symbol=symbol,
                signal='HOLD',
                sentiment_score=0.0,
                confidence=0.0,
                reasoning='Scaffold mock — no runner attached.',
            )
            for symbol in self._config.symbol_keys()
        ]
        return AnalysisEnvelope(
            pipeline_id=self._config.pipeline_id,
            outcome_type=self._config.outcome_type,
            prompt_version=self._config.prompt.version,
            # The reason is known even here (ISSUE_87) — the scaffold really did run for it.
            trigger_reason=reason,
            timestamp=now,
            status='success',
            result=results,
            metadata=RunMetadata(model='mock'),
        )
