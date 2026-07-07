"""Abstract base for a text embedder."""
from abc import ABC, abstractmethod
from typing import List


class AbstractEmbedder(ABC):
    """Turns text into a dense vector for similarity search."""

    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts.

        Args:
            texts: Raw text strings.

        Returns:
            One vector per input text, order-aligned with `texts`.
        """
        ...
