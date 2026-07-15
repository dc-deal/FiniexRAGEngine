"""Console rendering of a single symbol evaluation — the `eval` CLI's output surface.

Sits with the other reporting units rather than in the evaluator: producing an evaluation and
painting one are different jobs, and the engine must not depend on a console (the twin of
`envelope_report`, which renders a whole run). Ends on the shared `RunFooter`, so a spending
pass reports its own cost where it runs (CLAUDE.md).
"""
import textwrap
from typing import Optional

from finiexragengine.core.observability.run_footer import RunFooter
from finiexragengine.types.eval_types import SymbolEval


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
