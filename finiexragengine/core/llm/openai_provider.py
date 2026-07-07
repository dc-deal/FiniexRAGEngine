"""OpenAI-backed LLM provider (chat-completions + structured outputs)."""
from typing import Any, Dict

from finiexragengine.core.llm.abstract_llm_provider import AbstractLLMProvider
from finiexragengine.types.config_types.app_config_types import LlmConfig


class OpenAIProvider(AbstractLLMProvider):
    """Calls the OpenAI chat-completions API with structured outputs.

    TODO(impl): openai client from OPENAI_API_KEY; response_format=json_schema;
    low temperature (config); enforce timeout_seconds; raise LLMError on
    failure / parse error.
    """

    def __init__(self, config: LlmConfig, api_key: str) -> None:
        self._config = config
        self._api_key = api_key

    def complete_structured(self, prompt: str, json_schema: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError('OpenAIProvider.complete_structured')
