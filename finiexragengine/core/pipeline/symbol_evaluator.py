"""Single-symbol evaluation: retrieve -> prompt -> structured LLM -> enriched result.

The reusable per-symbol unit of the eval flow (ISSUE_6/7): it times each stage into
StageTiming (ISSUE_12) and attaches provenance from the *real* retrieved articles — the
LLM scores only the mood, never invents sources. ISSUE_7 orchestrates this over all
symbols into the outcome envelope.
"""
import logging
import textwrap
from typing import List, Optional

from pydantic import ValidationError

from finiexragengine.core.llm.abstract_llm_provider import AbstractLLMProvider
from finiexragengine.core.llm.prompt_builder import PromptBuilder
from finiexragengine.core.observability.run_footer import RunFooter
from finiexragengine.core.observability.stage_timer import StageTimer
from finiexragengine.core.rag.retriever import Retriever
from finiexragengine.exceptions.ragengine_errors import LLMParseError
from finiexragengine.types.eval_types import SymbolEval
from finiexragengine.types.llm_types import LlmUsage
from finiexragengine.types.outcome_types import (
    ArticleRef,
    SentimentLlmOutput,
    SentimentResult,
)
from finiexragengine.types.prompt_metadata import PromptMetadata

logger = logging.getLogger(__name__)


class SymbolEvaluator:
    """Evaluates one symbol: retrieve -> build prompt -> structured LLM -> enrich."""

    def __init__(self, retriever: Retriever, prompt_builder: PromptBuilder,
                 provider: AbstractLLMProvider, prompt_name: str = 'sentiment',
                 prompt_version: str = '1', breaking_threshold: float = 0.8) -> None:
        self._retriever = retriever
        self._prompt_builder = prompt_builder
        self._provider = provider
        self._prompt_name = prompt_name
        self._prompt_version = prompt_version
        self._breaking_threshold = breaking_threshold

    def evaluate(self, symbol: str, query: str) -> SymbolEval:
        # Every stage is timed (ISSUE_32) — the shared StageTimer collects the records.
        timer = StageTimer()
        context = timer.time('retrieve', lambda: self._retriever.retrieve(query))
        articles = context.articles
        # The prompt's front-matter identity travels with the outcome (ISSUE_33) — cached.
        prompt_metadata = self._prompt_builder.metadata(self._prompt_name, self._prompt_version)
        # Empty-context shortcut (ISSUE_24): the floor left nothing on-topic, so there is
        # nothing to evaluate — answer mechanically (contract row, tagged basis='no_data')
        # instead of paying the LLM to read generic articles. Logged for traceability;
        # deliberately *not* a RunError — no data is a legitimate outcome, the run stays
        # 'success'. The envelope proves it anyway: 0 tokens, empty raw output.
        if not articles:
            # The funnel says *why* it is empty (ISSUE_24): empty window vs floor cut —
            # and how close the nearest miss came (calibration signal for the floor).
            funnel = context.funnel
            nearest = (f'{funnel.best_distance:.3f}' if funnel.best_distance is not None
                       else 'n/a')
            logger.info("[NO_CONTEXT] %s ('%s'): %d in window, floor dropped %d "
                        '(nearest %s) — mechanical HOLD, no LLM call',
                        symbol, query, funnel.in_window, funnel.floor_dropped, nearest)
            result = SentimentResult(
                symbol=symbol, signal='HOLD', sentiment_score=0.0, confidence=0.0,
                reasoning='No relevant news found', basis='no_data')
            return SymbolEval(result=result, prompt='', prompt_metadata=prompt_metadata,
                              usage=LlmUsage(0, 0), articles=[],
                              stage_timings=timer.timings, raw_output={},
                              retrieval=context.funnel)
        # The prompt describes the asset in readable terms (the query, e.g. "Bitcoin BTC");
        # the result keys on the raw ticker `symbol` (e.g. "BTCUSD").
        prompt = timer.time('prompt', lambda: self._prompt_builder.build(
            self._prompt_name, self._prompt_version, query, articles))
        completion = timer.time('llm', lambda: self._provider.complete_structured(
            prompt, SentimentLlmOutput.model_json_schema()))

        # Validate the scored subset; a schema mismatch is a parse failure (LLM_PARSE_ERROR).
        try:
            scored = SentimentLlmOutput(**completion.data)
        except ValidationError as exc:
            raise LLMParseError(f'LLM output failed schema validation: {exc}') from exc

        # Enrich to the outcome: attach real provenance + the breaking flag (ISSUE_2/11).
        result = SentimentResult(
            symbol=symbol, signal=scored.signal, sentiment_score=scored.sentiment_score,
            confidence=scored.confidence, reasoning=scored.reasoning, urgency=scored.urgency,
            is_breaking=scored.urgency >= self._breaking_threshold,
            sources=[ArticleRef(article_id=a.article_id, url=a.url, title=a.title,
                                published_at=a.published_at, fetched_at=a.fetched_at)
                     for a in articles])
        return SymbolEval(result=result, prompt=prompt, prompt_metadata=prompt_metadata,
                          usage=completion.usage, articles=articles,
                          stage_timings=timer.timings, raw_output=completion.data,
                          model_snapshot=completion.model, retrieval=context.funnel)
