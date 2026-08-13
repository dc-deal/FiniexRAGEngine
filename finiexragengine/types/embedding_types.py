"""Embedding-side domain types — what one embed call produced (ISSUE_79).

The shape crosses the `AbstractEmbedder` seam to two consumers: the `Ingestor` (which stamps the
per-article counts onto the rows it stores) and the `QueryVectorCache` (which embeds one symbol
query). Behaviour lives in `core/rag/`; only the shape lives here.
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EmbedResult:
    """One `embed()` call's outcome — vectors plus what it cost and what it could not do.

    Replaced a bare `List[List[float]]` when the call gained a second and third thing to report
    (CLAUDE.md: a stage boundary returns a result object, and a bare return that needs another
    value gets converted rather than bolted onto a tuple).

    **Every list is index-aligned with the input `texts`**, including the failures — that is what
    lets the caller attribute a count or a rejection to the right article. A `None` vector means
    the provider refused that item outright; it is the only case where a caller must skip rather
    than store.
    """
    vectors: List[Optional[List[float]]] = field(default_factory=list)
    input_tokens: List[Optional[int]] = field(default_factory=list)      # what was sent
    truncated_tokens: List[Optional[int]] = field(default_factory=list)  # None = untouched
    rejected: List[int] = field(default_factory=list)                    # refused input indices
    # The provider's own count, summed across batches. Kept next to our own sum so the two can be
    # compared — the running check that our tokenizer still agrees with theirs.
    billed_tokens: int = 0

    @property
    def truncated_count(self) -> int:
        return sum(1 for dropped in self.truncated_tokens if dropped is not None)

    @property
    def counted_tokens(self) -> int:
        """Our own token sum over everything actually sent — the cross-check's left-hand side."""
        return sum(tokens for tokens in self.input_tokens if tokens is not None)
