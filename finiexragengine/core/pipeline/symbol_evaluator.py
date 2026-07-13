"""Single-symbol evaluation: retrieve -> prompt -> structured LLM -> enriched result.

The reusable per-symbol unit of the eval flow (ISSUE_6/7): it times each stage into
StageTiming (ISSUE_12) and attaches provenance from the *real* retrieved articles — the
LLM scores only the mood, never invents sources. ISSUE_7 orchestrates this over all
symbols into the outcome envelope.
"""
import logging
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from finiexragengine.core.llm.abstract_llm_provider import AbstractLLMProvider
from finiexragengine.core.llm.prompt_builder import PromptBuilder
from finiexragengine.core.observability.run_footer import RunFooter
from finiexragengine.core.observability.stage_timer import StageTimer
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
from finiexragengine.types.prompt_metadata import PromptMetadata

logger = logging.getLogger(__name__)


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
        articles = timer.time('retrieve', lambda: self._retriever.retrieve(query))
        # The prompt's front-matter identity travels with the outcome (ISSUE_33) — cached.
        prompt_metadata = self._prompt_builder.metadata(self._prompt_name, self._prompt_version)
        # Empty-context shortcut (ISSUE_24): the floor left nothing on-topic, so there is
        # nothing to evaluate — answer mechanically (contract row, tagged basis='no_data')
        # instead of paying the LLM to read generic articles. Logged for traceability;
        # deliberately *not* a RunError — no data is a legitimate outcome, the run stays
        # 'success'. The envelope proves it anyway: 0 tokens, empty raw output.
        if not articles:
            logger.info("[NO_CONTEXT] %s ('%s'): retrieval empty after floor — "
                        'mechanical HOLD, no LLM call', symbol, query)
            result = SentimentResult(
                symbol=symbol, signal='HOLD', sentiment_score=0.0, confidence=0.0,
                reasoning='No relevant news found', basis='no_data')
            return SymbolEval(result=result, prompt='', prompt_metadata=prompt_metadata,
                              usage=LlmUsage(0, 0), articles=[],
                              stage_timings=timer.timings, raw_output={})
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
                          model_snapshot=completion.model)


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


def format_symbol_eval(ev: SymbolEval, pipeline_id: str, usd: Optional[float] = None, *,
                       model: str = '', prompt_cols: int = 60, prompt_lines: int = 4,
                       full_prompt: bool = False) -> str:
    """Render a SymbolEval as the console signal card + a compacted prompt excerpt."""
    r = ev.result
    m = ev.prompt_metadata
    titles = ', '.join(s.title[:34] for s in r.sources[:3])
    reasoning = textwrap.fill(r.reasoning, width=64, subsequent_indent=' ' * 14)
    # Model line: configured name + how it resolved — '(pinned)' when the config names
    # the exact snapshot, '(served …)' when an alias was resolved, no_data when no call ran.
    if model and ev.model_snapshot:
        resolved = '(pinned)' if ev.model_snapshot == model else f'(served {ev.model_snapshot})'
        model_label = f'{model} {resolved}'
    elif model:
        model_label = f'{model} (not called — no_data)' if not ev.prompt else model
    else:
        model_label = ''
    # The shared metrics block (ISSUE_32) — same pattern as the ingest footer.
    footer = RunFooter(
        timings=ev.stage_timings,
        tokens_label=f'prompt {ev.usage.prompt_tokens} · completion {ev.usage.completion_tokens} '
                     f'· total {ev.usage.total_tokens}',
        usd=usd, section='llm_eval', model_label=model_label)
    lines = [
        f"=== Signal: {r.symbol}   (pipeline {pipeline_id} · "
        f"prompt {m.id}@v{m.version} #{m.content_hash}) ===",
        f'  signal      {r.signal}',
        f'  score       {r.sentiment_score:+.2f}    confidence {r.confidence:.2f}    '
        f'urgency {r.urgency:.2f}    breaking {"yes" if r.is_breaking else "no"}',
        f'  reasoning   {reasoning}',
        f'  sources     {len(r.sources)} articles  ({titles})',
        '',
        footer.render(),
        '',
    ]
    if not ev.prompt:
        # no_data shortcut (ISSUE_24): no prompt was built, no LLM call was made.
        lines.append('--- prompt ' + '-' * 53)
        lines.append('  (no context after floor — LLM call skipped, basis=no_data)')
    elif full_prompt:
        lines.append('--- prompt sent (full) ' + '-' * 40)
        lines.append(ev.prompt)
    else:
        lines.append(f'--- prompt sent (rendered, compacted · {prompt_cols} col) ' + '-' * 18)
        lines.append(_compact_prompt(ev.prompt, prompt_cols, prompt_lines))
    lines.append('-' * 64)
    return '\n'.join(lines)
