"""Run footer — the shared timings/tokens/cost echo block of every paid pass (ISSUE_23/32)."""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from finiexragengine.types.outcome_types import StageTiming


@dataclass
class RunFooter:
    """Renders the per-run metrics section every spending CLI prints.

    The pattern block of the cost/perf system: a `--- run metrics ---` divider, one
    `timings` line (per stage + total) and one `tokens` line (usage + derived cost +
    section). Every pass that spends time or budget appends this, so the pain point
    (slow API call, silent spend) is visible right where the run happened — the durable
    warehouse stays the cost_log / persisted envelopes.

    `aggregate` collapses repeated stage names (e.g. one 'fetch' per source in an ingest
    pass) into a single summed entry — the display stays compact while the raw timings
    stay intact for the envelope (ISSUE_7).
    """
    timings: List[StageTiming]
    tokens_label: str                 # e.g. 'prompt 1975 · completion 58 · total 2033' / '8,734 embedding'
    usd: Optional[float] = None       # None -> no cost suffix (nothing was paid)
    section: str = ''                 # billing section the cost was logged under
    model_label: str = ''             # which model ran, e.g. 'gpt-4o-mini (served gpt-4o-mini-2024-07-18)'
    aggregate: bool = False
    width: int = 64

    def _stage_ms(self) -> List[Tuple[str, float]]:
        if not self.aggregate:
            return [(t.stage, t.duration_ms) for t in self.timings]
        # Sum per stage name, first-appearance order (fetch -> embed -> upsert).
        summed: Dict[str, float] = {}
        for t in self.timings:
            summed[t.stage] = summed.get(t.stage, 0.0) + t.duration_ms
        return list(summed.items())

    def render(self) -> str:
        title = '--- run metrics '
        lines = [title + '-' * max(0, self.width - len(title))]
        # The model always shows where money was spent — a print that costs tokens
        # names what produced them (configured alias + served snapshot when known).
        if self.model_label:
            lines.append(f'  model       {self.model_label}')
        stages = ' · '.join(f'{stage} {ms:.0f}ms' for stage, ms in self._stage_ms())
        if stages:
            total = sum(t.duration_ms for t in self.timings)
            lines.append(f'  timings     {stages} · total {total:.0f}ms')
        else:
            lines.append('  timings     (no stages ran)')
        cost = f'   cost ${self.usd:.6f} ({self.section})' if self.usd is not None else ''
        lines.append(f'  tokens      {self.tokens_label}{cost}')
        return '\n'.join(lines)
