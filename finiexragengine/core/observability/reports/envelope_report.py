"""Console rendering of a run envelope — the `run` CLI's output surface.

Sits with the other reporting units rather than in the runner: producing an envelope and
painting one are different jobs, and the engine must not depend on a console. Renders the
shared console pattern (title + aligned columns + the `--- run metrics ---` footer), so a
spending pass reports its own cost where it runs (CLAUDE.md).
"""
from finiexragengine.core.observability.run_footer import RunFooter
from finiexragengine.types.outcome_types import AnalysisEnvelope


def format_envelope_run(envelope: AnalysisEnvelope) -> str:
    """Render a full run envelope as the console pattern: header, signal table, metrics."""
    m = envelope.metadata
    fingerprint = (f'prompt {envelope.prompt_id}@v{envelope.prompt_version} '
                   f'#{envelope.prompt_hash}' if envelope.prompt_id else 'prompt (mock)')
    lines = [
        f'=== Run: {envelope.pipeline_id}   ({envelope.outcome_type} · {fingerprint}) ===',
        f'  status      {envelope.status}     sources {m.sources_reached}/{m.sources_configured}'
        f'   articles {m.articles_found} found · {m.articles_relevant} relevant',
        '',
        f'  {"symbol":10} {"signal":6} {"score":>6} {"conf":>5} {"urg":>5}  brk  sources  basis',
    ]
    for r in envelope.result:
        lines.append(f'  {r.symbol:10} {r.signal:6} {r.sentiment_score:>+6.2f} '
                     f'{r.confidence:>5.2f} {r.urgency:>5.2f}  {"yes" if r.is_breaking else "no ":3} '
                     f'{len(r.sources):>7}  {r.basis}')
    if envelope.errors:
        lines.append('')
        for error in envelope.errors:
            lines.append(f'  ERROR       [{error.type}] {error.message}')
    if m.model_snapshot == m.model and m.model_snapshot:
        model_label = f'{m.model} (pinned)'
    elif m.model_snapshot:
        model_label = f'{m.model} (served {m.model_snapshot})'
    else:
        model_label = m.model
    footer = RunFooter(
        timings=m.stage_timings,
        tokens_label=f'prompt {m.prompt_tokens} · completion {m.completion_tokens} '
                     f'· total {m.prompt_tokens + m.completion_tokens}',
        usd=m.cost_usd, section='this run', model_label=model_label, aggregate=True)
    lines += ['', footer.render()]
    return '\n'.join(lines)
