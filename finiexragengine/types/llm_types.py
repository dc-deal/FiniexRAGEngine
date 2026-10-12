"""LLM call result types — the structured payload plus the call's token usage (ISSUE_6).

`usage` is captured at the call (irreconstructable afterwards) and priced by the
CostRecorder (ISSUE_23), so every LLM eval reports its spend.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, Literal, Optional, Tuple, TypeVar, get_args

T = TypeVar('T')

# How one attempt at a provider call ended (ISSUE_128). Strict where a `cost_log` row is built; the
# column itself is plain TEXT, so a value a later version adds still reads back.
#   ok            the call answered — tokens and USD are known
#   timeout       our per-attempt deadline, or the server's 408/409 — retried
#   connection    no answer at all (DNS, TCP, TLS) — retried
#   rate_limited  a throttling 429 — retried, honouring `Retry-After`
#   server_error  5xx — retried
#   rejected      any other 4xx, or an error without a status — never retried, it would fail again
#   quota         the account's spend limit — never retried, handed to the budget breaker
AttemptOutcome = Literal['ok', 'timeout', 'connection', 'rate_limited', 'server_error',
                         'rejected', 'quota']
ATTEMPT_OUTCOMES: Tuple[str, ...] = get_args(AttemptOutcome)


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


@dataclass(frozen=True)
class ModelProfile:
    """What a model family accepts and needs at the call — a property of the vendor's API, per family.

    Resolved by `ModelProfiles` from a model id. Decides how the provider calls a model, never what
    the pipeline asks it: the series-defining choice of model stays in the pipeline config.
    """
    family: str                   # the matched prefix ('gpt-5', 'gpt-4o'), or 'default'
    accepts_temperature: bool     # sending `temperature` to a family that rejects it is a hard 400
    strict_output: bool           # enforce the JSON schema at decoding (OpenAI `strict: true`)
    reasoning: bool               # hidden reasoning tokens: slower, billed as output
    # Per-attempt ceiling for this family (ISSUE_128); None = the global `llm.timeout_seconds`.
    call_timeout_seconds: Optional[float] = None

    def describe(self) -> str:
        """The boot-line wording.

        `family gpt-5 · reasoning · strict output · no temperature · timeout 120 s`
        """
        parts = [f'family {self.family}']
        if self.reasoning:
            parts.append('reasoning')
        if self.strict_output:
            parts.append('strict output')
        parts.append('temperature' if self.accepts_temperature else 'no temperature')
        if self.call_timeout_seconds is not None:
            parts.append(f'timeout {self.call_timeout_seconds:g} s')
        return ' · '.join(parts)


@dataclass
class AttemptSuccess(Generic[T]):
    """The answer of a call that needed one or more attempts (ISSUE_128).

    `attempt` is 1-based — which attempt answered — and `duration_ms` is that attempt alone: the
    latency sample of the call that succeeded, never the sum with the failed ones before it, which
    are recorded as rows of their own.
    """
    value: T
    attempt: int
    duration_ms: float

