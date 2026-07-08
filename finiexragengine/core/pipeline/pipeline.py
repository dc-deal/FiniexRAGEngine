"""Pipeline — orchestrates one constellation end-to-end."""
from datetime import datetime, timezone
from typing import List

from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.outcome_types import (
    AnalysisEnvelope,
    RunMetadata,
    SentimentResult,
)


class Pipeline:
    """Runs one configured pipeline end-to-end and returns a typed envelope.

    Real flow (TODO impl — see the ISSUEs):
        1. fetch articles from each Source            (stage 'fetch', timed — ISSUE_7)
        2. embed + upsert into the vector store        (stage 'embed', idempotent — ISSUE_3)
        3. per symbol: Retriever.retrieve(...)         (stage 'retrieve', recency+dedup — ISSUE_3)
        4. build prompt + LLMProvider.complete_structured (stage 'llm')
        5. parse -> SentimentResult[] with provenance  (stage 'parse', citations — ISSUE_2)
        6. set urgency/is_breaking; persist to the OutcomeStore (source of truth)

    The scaffold's run() returns a deterministic MOCK envelope so the service
    boots and the API contract is exercisable before the stages are implemented.
    """

    def __init__(self, config: PipelineConfig) -> None:
        self._config = config

    def get_config(self) -> PipelineConfig:
        return self._config

    def run(self) -> AnalysisEnvelope:
        """Execute the pipeline once and return its outcome envelope.

        Returns:
            A mock envelope (scaffold). Replace with the real staged flow above.
        """
        now = datetime.now(timezone.utc)
        # Envelope invariant: every requested symbol is always present in the result —
        # even the scaffold mock emits one HOLD/0.0 entry per configured symbol, never a gap.
        results: List[SentimentResult] = [
            SentimentResult(
                symbol=symbol,
                signal='HOLD',
                sentiment_score=0.0,
                confidence=0.0,
                reasoning='Scaffold mock — pipeline stages not yet implemented.',
            )
            for symbol in self._config.symbols
        ]
        return AnalysisEnvelope(
            pipeline_id=self._config.pipeline_id,
            outcome_type=self._config.outcome_type,
            prompt_version=self._config.prompt_version,
            timestamp=now,
            status='success',
            result=results,
            metadata=RunMetadata(model='mock'),
        )
