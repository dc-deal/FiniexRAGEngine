"""Abstract base for the LLM provider."""
from abc import ABC, abstractmethod
from typing import Any, Dict


class AbstractLLMProvider(ABC):
    """Provider-agnostic LLM access (OpenAI chat-completions format).

    Backends speaking the same format (OpenAI API, local vLLM/Ollama) are
    interchangeable behind this contract.
    """

    @abstractmethod
    def complete_structured(self, prompt: str, json_schema: Dict[str, Any]) -> Dict[str, Any]:
        """Run a completion that must return JSON matching `json_schema`.

        Args:
            prompt: The fully-built prompt (constructed from retrieved context).
            json_schema: The expected output schema (structured outputs).

        Returns:
            The parsed JSON object.
        """
        ...
