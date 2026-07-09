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
    """A structured LLM completion: the parsed JSON payload + the call's token usage."""
    data: Dict[str, Any]
    usage: LlmUsage = field(default_factory=LlmUsage)
