"""Pipeline — the registry-facing handle for one constellation."""
from datetime import datetime, timezone
from typing import List, Optional

from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.outcome_types import (
    AnalysisEnvelope,
    RunMetadata,
    SentimentResult,
)


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
        self._runner: Optional['PipelineRunner'] = None  # noqa: F821 — set via set_runner

    def get_config(self) -> PipelineConfig:
        return self._config

    def set_runner(self, runner) -> None:
        """Attach the real staged runner (built by the PipelineAssembler, ISSUE_7)."""
        self._runner = runner

    def run(self) -> AnalysisEnvelope:
        """Execute the pipeline once and return its outcome envelope."""
        if self._runner is not None:
            return self._runner.run()
        return self._mock_envelope()

    def _mock_envelope(self) -> AnalysisEnvelope:
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
            for symbol in self._config.symbols
        ]
        return AnalysisEnvelope(
            pipeline_id=self._config.pipeline_id,
            outcome_type=self._config.outcome_type,
            prompt_version=self._config.prompt.version,
            timestamp=now,
            status='success',
            result=results,
            metadata=RunMetadata(model='mock'),
        )
