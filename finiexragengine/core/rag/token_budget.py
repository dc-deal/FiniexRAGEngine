"""Fits text to an embedding model's input limit (ISSUE_79).

The corpus lost articles for ~30 hours because one over-long item made the whole embed batch
return HTTP 400, which failed the entire ingest pass — and the item, never stored, came back every
pass until its feed dropped it. This unit removes the cause: measure exactly, trim to the limit,
and report both numbers so the trim is never silent.

**Why exact counting and not a rule of thumb.** "About four characters per token" holds for English
prose and collapses to one or two on URLs, number columns and CJK — precisely the content that blew
the limit. And the trim count is written to the corpus as an analysis field, so an estimate there
would be worse than no number at all.

**Tokenization is not a model decision.** It happens before the model, deterministically: a fixed
BPE vocabulary maps text to the integer ids the model actually sees. tiktoken ships OpenAI's own
algorithm and tables, so a local count equals the provider's — a claim the embedder re-checks
against every response's reported usage rather than trusting it forever.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import tiktoken

from finiexragengine.exceptions.ragengine_errors import EmbeddingError
from finiexragengine.types.config_types.app_config_types import EmbeddingConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FittedText:
    """One text measured against the limit — and trimmed if it exceeded it.

    Built and consumed inside the rag domain, so it stays with this unit rather than in `types/`.
    `dropped_tokens` is None when the text fit; both counts are byproducts of the encode the
    limit check performs anyway, so neither costs an extra pass over the text.
    """
    text: str
    tokens: int                          # what will actually be sent
    dropped_tokens: Optional[int] = None  # None = untouched; else how many tokens were cut


class TokenBudget:
    """Counts and trims text against one embedding model's input limit.

    Lives in `core/rag/` with the embedder, not on the sources path: the limit is a property of
    the *model*, and #16 binds the corpus to exactly one — so every source-set shares it, and
    acquisition has no business knowing about tokens.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self._max_tokens = config.max_input_tokens
        # Resolve now, not on first use: an unknown model must fail at boot (where the startup
        # check surfaces it) rather than inside a worker thread mid-pass. The mapping is tiktoken's
        # own table — the OpenAI API cannot answer this, `/v1/models` carries no encoding field.
        try:
            self._encoding = (tiktoken.get_encoding(config.encoding) if config.encoding
                              else tiktoken.encoding_for_model(config.model))
        except KeyError as exc:
            raise EmbeddingError(
                f"no tokenizer for embedding model '{config.model}' — tiktoken does not know it "
                f'yet. Set `embedding.encoding` explicitly (e.g. "cl100k_base") or upgrade '
                f'tiktoken.') from exc

    @property
    def max_tokens(self) -> int:
        return self._max_tokens

    def fit(self, text: str) -> FittedText:
        """Measure `text`; trim it to the limit if it overruns.

        The cut happens in token space and is decoded back, so it can never land inside a token —
        a character-level cut could, and the tail would then re-tokenize differently than counted.
        """
        ids = self._encoding.encode(text)
        if len(ids) <= self._max_tokens:
            return FittedText(text=text, tokens=len(ids))
        kept = ids[:self._max_tokens]
        return FittedText(text=self._encoding.decode(kept), tokens=len(kept),
                          dropped_tokens=len(ids) - self._max_tokens)
