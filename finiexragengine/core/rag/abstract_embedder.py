"""Abstract base for a text embedder."""
from abc import ABC, abstractmethod
from typing import List

from finiexragengine.types.embedding_types import EmbedResult


class AbstractEmbedder(ABC):
    """Turns text into a dense vector for similarity search."""

    @abstractmethod
    def embed(self, texts: List[str]) -> EmbedResult:
        """Embed a batch of texts.

        Args:
            texts: Raw text strings. An implementation may trim a text to its model's input
                limit; it must then report that on the result rather than trimming silently.

        Returns:
            An `EmbedResult` whose lists are all index-aligned with `texts` — one entry per input,
            in order, including for inputs the provider refused (vector `None`). A bare vector list
            could not carry the per-item truncation counts and rejections the ingest path stores
            (ISSUE_79).
        """
        ...
