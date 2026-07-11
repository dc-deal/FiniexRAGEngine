"""LLM call result types — the structured payload plus the call's token usage (ISSUE_6).

`usage` is captured at the call (irreconstructable afterwards) and priced by the
CostRecorder (ISSUE_23), so every LLM eval reports its spend.
"""
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class LlmUsage:
    """Token usage of one LLM call — the cost basis."""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LlmCompletion:
    """A structured LLM completion: the parsed JSON payload + the call's token usage.

    `model` is the *served* model as reported by the API (`response.model`, e.g.
    'gpt-4o-mini-2024-07-18') — not the configured alias. Aliases are retargeted
    silently by the provider; capturing the dated snapshot makes such a switch visible
    in the series, exactly like the prompt hash does for template edits (ISSUE_33).
    """
    data: Dict[str, Any]
    usage: LlmUsage = field(default_factory=LlmUsage)
    model: str = ''
