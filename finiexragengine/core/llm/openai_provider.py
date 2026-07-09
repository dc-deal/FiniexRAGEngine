"""OpenAI-backed LLM provider (chat-completions + structured outputs)."""
import json
from typing import TYPE_CHECKING, Any, Dict, Optional

from openai import APITimeoutError, OpenAI, OpenAIError

from finiexragengine.core.llm.abstract_llm_provider import AbstractLLMProvider
from finiexragengine.exceptions.ragengine_errors import (
    LLMApiError,
    LLMParseError,
    LLMTimeoutError,
)
from finiexragengine.types.config_types.app_config_types import LlmConfig
from finiexragengine.types.llm_types import LlmCompletion, LlmUsage

if TYPE_CHECKING:
    from finiexragengine.core.observability.cost_recorder import CostRecorder


class OpenAIProvider(AbstractLLMProvider):
    """Calls the OpenAI chat-completions API with a JSON-schema response format.

    Low temperature + timeout come from `LlmConfig`. Failures map to the LLMError
    taxonomy: timeout -> LLMTimeoutError, backend error -> LLMApiError, non-JSON output
    -> LLMParseError. Token usage is captured on every call (ISSUE_23) and, if a
    cost_recorder is set, logged under `section` (default 'llm_eval').
    """

    def __init__(self, config: LlmConfig, api_key: Optional[str] = None,
                 client: Optional[OpenAI] = None,
                 cost_recorder: Optional['CostRecorder'] = None,
                 section: str = 'llm_eval', pipeline_id: Optional[str] = None) -> None:
        self._config = config
        self._api_key = api_key
        self._client = client   # built lazily from OPENAI_API_KEY / api_key if not injected
        self._cost_recorder = cost_recorder
        self._section = section
        self._pipeline_id = pipeline_id

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = OpenAI(api_key=self._api_key) if self._api_key else OpenAI()
        return self._client

    def complete_structured(self, prompt: str, json_schema: Dict[str, Any]) -> LlmCompletion:
        client = self._get_client()
        try:
            response = client.chat.completions.create(
                model=self._config.model,
                messages=[{'role': 'user', 'content': prompt}],
                # Structured output: the model must return JSON matching the schema.
                # strict=False accepts the full Pydantic schema (range constraints and
                # all); the caller validates the payload against its model on top.
                response_format={
                    'type': 'json_schema',
                    'json_schema': {'name': 'structured_output',
                                    'schema': json_schema, 'strict': False},
                },
                temperature=self._config.temperature,
                timeout=self._config.timeout_seconds,
            )
        except APITimeoutError as exc:   # subclass of OpenAIError — catch first
            raise LLMTimeoutError(f'LLM call timed out: {exc}') from exc
        except OpenAIError as exc:
            raise LLMApiError(f'LLM backend error: {exc}') from exc

        content = response.choices[0].message.content or ''
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMParseError(f'LLM returned non-JSON output: {exc}') from exc

        # Capture the paid usage (ISSUE_23) — cost is never silent.
        raw = getattr(response, 'usage', None)
        usage = LlmUsage(
            prompt_tokens=getattr(raw, 'prompt_tokens', 0) or 0,
            completion_tokens=getattr(raw, 'completion_tokens', 0) or 0)
        if self._cost_recorder is not None and (usage.prompt_tokens or usage.completion_tokens):
            self._cost_recorder.record(self._section, self._config.model,
                                       usage.prompt_tokens, usage.completion_tokens,
                                       self._pipeline_id)
        return LlmCompletion(data=data, usage=usage)
