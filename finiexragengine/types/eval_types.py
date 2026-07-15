"""Eval-side domain types — one symbol's evaluation and what it took.

The shape the `SymbolEvaluator` produces and the `PipelineRunner` folds into the envelope.
Behaviour lives in `core/pipeline/`; only the shape lives here.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

from finiexragengine.types.article_types import Article
from finiexragengine.types.llm_types import LlmUsage
from finiexragengine.types.outcome_types import SentimentResult, StageTiming
from finiexragengine.types.prompt_metadata import PromptMetadata


@dataclass
class SymbolEval:
    """One symbol's evaluation — the result plus what it took (prompt, usage, timings)."""
    result: SentimentResult
    prompt: str
    prompt_metadata: PromptMetadata           # which prompt produced this (ISSUE_33)
    usage: LlmUsage
    articles: List[Article]
    stage_timings: List[StageTiming]
    # The raw scored JSON exactly as the model returned it (ISSUE_36) — irreconstructable
    # after the call; persisted next to the normalized envelope by the outcome store (ISSUE_8).
    raw_output: Dict[str, Any] = field(default_factory=dict)
    # The *served* model (response.model, the dated snapshot) — '' when no LLM ran.
    model_snapshot: str = ''

    def total_ms(self) -> float:
        return sum(timing.duration_ms for timing in self.stage_timings)
