"""Abstract base for the LLM provider."""
from abc import ABC, abstractmethod
from typing import Any, Dict

from finiexragengine.types.llm_types import LlmCompletion


class AbstractLLMProvider(ABC):
    """Provider-agnostic LLM access (OpenAI chat-completions format).

    Backends speaking the same format (OpenAI API, local vLLM/Ollama) are
    interchangeable behind this contract.
    """

    @abstractmethod
    def complete_structured(self, prompt: str, json_schema: Dict[str, Any]) -> LlmCompletion:
        """Run a completion that must return JSON matching `json_schema`.

        Args:
            prompt: The fully-built prompt (constructed from retrieved context).
            json_schema: The expected output schema (structured outputs).

        Returns:
            The parsed JSON payload plus the call's token usage (LlmCompletion) — the
            usage is captured for cost accounting (ISSUE_23).
        """
        ...
