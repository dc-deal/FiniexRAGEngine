"""Single-symbol evaluation: retrieve -> prompt -> structured LLM -> enriched result.

The reusable per-symbol unit of the eval flow (ISSUE_6/7): it times each stage into
StageTiming (ISSUE_12) and attaches provenance from the *real* retrieved articles — the
LLM scores only the mood, never invents sources. ISSUE_7 orchestrates this over all
symbols into the outcome envelope.
"""
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable, List, Optional

from pydantic import ValidationError

from finiexragengine.core.llm.abstract_llm_provider import AbstractLLMProvider
from finiexragengine.core.llm.prompt_builder import PromptBuilder
from finiexragengine.core.rag.retriever import Retriever
from finiexragengine.exceptions.ragengine_errors import LLMParseError
from finiexragengine.types.article_types import Article
from finiexragengine.types.llm_types import LlmUsage
from finiexragengine.types.outcome_types import (
    ArticleRef,
    SentimentLlmOutput,
    SentimentResult,
    StageTiming,
)


@dataclass
class SymbolEval:
    """One symbol's evaluation — the result plus what it took (prompt, usage, timings)."""
    result: SentimentResult
    prompt: str
    usage: LlmUsage
    articles: List[Article]
    stage_timings: List[StageTiming]

    def total_ms(self) -> float:
        return sum(timing.duration_ms for timing in self.stage_timings)


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
        timings: List[StageTiming] = []
        articles = self._timed('retrieve', timings, lambda: self._retriever.retrieve(query))
        # The prompt describes the asset in readable terms (the query, e.g. "Bitcoin BTC");
        # the result keys on the raw ticker `symbol` (e.g. "BTCUSD").
        prompt = self._timed('prompt', timings, lambda: self._prompt_builder.build(
            self._prompt_name, self._prompt_version, query, articles))
        completion = self._timed('llm', timings, lambda: self._provider.complete_structured(
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
                                published_at=a.published_at) for a in articles])
        return SymbolEval(result=result, prompt=prompt, usage=completion.usage,
                          articles=articles, stage_timings=timings)

    def _timed(self, stage: str, timings: List[StageTiming], fn: Callable):
        started = datetime.now(timezone.utc)
        start = perf_counter()
        value = fn()
        duration_ms = (perf_counter() - start) * 1000.0
        timings.append(StageTiming(stage=stage, started_at=started,
                                   ended_at=datetime.now(timezone.utc), duration_ms=duration_ms))
        return value


def _compact_prompt(prompt: str, cols: int, lines: int) -> str:
    """Rendered prompt, compacted: newlines -> ⏎, hard-wrapped to `cols`, capped at `lines`."""
    collapsed = prompt.replace('\n', '⏎')
    chunks = [collapsed[i:i + cols] for i in range(0, len(collapsed), cols)]
    shown = chunks[:lines]
    rendered = '\n'.join('  ' + chunk for chunk in shown)
    remaining = len(collapsed) - sum(len(chunk) for chunk in shown)
    if remaining > 0:
        rendered += f'\n  [+{remaining} chars]'
    return rendered


def format_symbol_eval(ev: SymbolEval, pipeline_id: str, prompt_name: str,
                       prompt_version: str, usd: Optional[float] = None, *,
                       prompt_cols: int = 60, prompt_lines: int = 4,
                       full_prompt: bool = False) -> str:
    """Render a SymbolEval as the console signal card + a compacted prompt excerpt."""
    r = ev.result
    titles = ', '.join(s.title[:34] for s in r.sources[:3])
    reasoning = textwrap.fill(r.reasoning, width=64, subsequent_indent=' ' * 14)
    timings = ' · '.join(f'{t.stage} {t.duration_ms:.0f}ms' for t in ev.stage_timings)
    cost = f'   cost ${usd:.6f} (llm_eval)' if usd is not None else ''
    lines = [
        f"=== Signal: {r.symbol}   (pipeline {pipeline_id} · prompt {prompt_name}_v{prompt_version}) ===",
        f'  signal      {r.signal}',
        f'  score       {r.sentiment_score:+.2f}    confidence {r.confidence:.2f}    '
        f'urgency {r.urgency:.2f}    breaking {"yes" if r.is_breaking else "no"}',
        f'  reasoning   {reasoning}',
        f'  sources     {len(r.sources)} articles  ({titles})',
        '',
        f'  timings     {timings} · total {ev.total_ms():.0f}ms',
        f'  tokens      prompt {ev.usage.prompt_tokens} · completion {ev.usage.completion_tokens} '
        f'· total {ev.usage.total_tokens}{cost}',
        '',
    ]
    if full_prompt:
        lines.append('--- prompt sent (full) ' + '-' * 40)
        lines.append(ev.prompt)
    else:
        lines.append(f'--- prompt sent (rendered, compacted · {prompt_cols} col) ' + '-' * 18)
        lines.append(_compact_prompt(ev.prompt, prompt_cols, prompt_lines))
    lines.append('-' * 64)
    return '\n'.join(lines)
