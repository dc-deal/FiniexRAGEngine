"""The envelope output contract — the fixed error taxonomy + the degraded HOLD row.

Deliberately its own unit rather than part of the runner: **both** producers of an envelope
depend on it — the `PipelineRunner` (a stage failed mid-pass) and the API's catch-all
(`sentiment_router`, an internal failure before any pass ran). Neither should have to import
the other's machinery to honour the contract, so the contract lives on its own.

The invariants it serves (CLAUDE.md "Engine output contract"): every requested symbol is always
present in `result` — a failed symbol degrades to a clean HOLD row, never a gap — and every
`RunError.type` comes from a fixed taxonomy, never a free string.
"""
from finiexragengine.exceptions.ragengine_errors import (
    BudgetExceededError,
    LLMApiError,
    LLMParseError,
    LLMTimeoutError,
    SourceFetchError,
    VectorStoreError,
)
from finiexragengine.types.outcome_types import SentimentResult

# The fixed RunError taxonomy (output contract): exact exception class -> type string.
# Anything unmapped degrades under PARTIAL_RESPONSE — the row-was-degraded marker.
_ERROR_TAXONOMY = {
    SourceFetchError: 'SOURCE_UNREACHABLE',
    LLMTimeoutError: 'LLM_TIMEOUT',
    LLMApiError: 'LLM_API_ERROR',
    LLMParseError: 'LLM_PARSE_ERROR',
    VectorStoreError: 'VECTOR_STORE_ERROR',
    BudgetExceededError: 'BUDGET_EXCEEDED',   # provider quota reached (ISSUE_47) — degrade to HOLD
}


def taxonomy_type(exc: Exception) -> str:
    """Map an engine exception to its fixed RunError.type (fallback: PARTIAL_RESPONSE)."""
    for exc_class, type_string in _ERROR_TAXONOMY.items():
        if isinstance(exc, exc_class):
            return type_string
    return 'PARTIAL_RESPONSE'


def hold_result(symbol: str, reasoning: str,
                basis: str = 'degraded') -> SentimentResult:
    """A contract HOLD row: HOLD / 0.0 / reason / no sources — never a missing symbol.

    `basis` tags how the row came to be (ISSUE_24/35): the runner/API use 'degraded'
    (a failure forced the HOLD); the evaluator's data-shortage shortcut emits its own
    row with 'no_data'.
    """
    return SentimentResult(symbol=symbol, signal='HOLD', sentiment_score=0.0,
                           confidence=0.0, reasoning=reasoning, basis=basis)
