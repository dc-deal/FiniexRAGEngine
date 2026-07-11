"""OpenAI-backed LLM provider (chat-completions + structured outputs)."""
import json
from time import perf_counter
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

    The eval `model` is an explicit argument — it comes from the *pipeline's* declared
    `llm.model` (series-defining, never a global default); `LlmConfig` contributes only
    the call mechanics (temperature, timeout, optional `base_url` for an OpenAI-compatible
    endpoint such as vLLM/Ollama — self-hosted or fine-tuned models ride the same seam).
    Failures map to the LLMError taxonomy: timeout -> LLMTimeoutError, backend error ->
    LLMApiError, non-JSON output -> LLMParseError. Token usage *and the served model*
    (`response.model`, the dated snapshot behind the alias) are captured on every call
    (ISSUE_23) and, if a cost_recorder is set, logged under `section`.
    """

    def __init__(self, config: LlmConfig, model: str, api_key: Optional[str] = None,
                 client: Optional[OpenAI] = None,
                 cost_recorder: Optional['CostRecorder'] = None,
                 section: str = 'llm_eval', pipeline_id: Optional[str] = None) -> None:
        self._config = config
        self._model = model
        self._api_key = api_key
        self._client = client   # built lazily from OPENAI_API_KEY / api_key if not injected
        self._cost_recorder = cost_recorder
        self._section = section
        self._pipeline_id = pipeline_id

    def _get_client(self) -> OpenAI:
        if self._client is None:
            # base_url switches to an OpenAI-compatible endpoint (user_configs override);
            # None keeps the official API. The key still comes from env / api_key.
            self._client = OpenAI(api_key=self._api_key, base_url=self._config.base_url)
        return self._client

    def complete_structured(self, prompt: str, json_schema: Dict[str, Any]) -> LlmCompletion:
        client = self._get_client()
        call_start = perf_counter()
        try:
            response = client.chat.completions.create(
                model=self._model,
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
        # Pure API time — the latency sample recorded next to the tokens (ISSUE_32).
        api_ms = (perf_counter() - call_start) * 1000.0

        content = response.choices[0].message.content or ''
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMParseError(f'LLM returned non-JSON output: {exc}') from exc

        # Capture the paid usage (ISSUE_23) and the *served* model: `response.model` is
        # the dated snapshot the alias actually resolved to — a silent alias retarget
        # becomes visible in the series, like a prompt-hash change (ISSUE_33).
        raw = getattr(response, 'usage', None)
        usage = LlmUsage(
            prompt_tokens=getattr(raw, 'prompt_tokens', 0) or 0,
            completion_tokens=getattr(raw, 'completion_tokens', 0) or 0)
        served_model = getattr(response, 'model', '') or ''
        if self._cost_recorder is not None and (usage.prompt_tokens or usage.completion_tokens):
            # Priced by the *configured* name (the pricing-table key); the snapshot is
            # stored alongside as the trace of what actually served the call.
            self._cost_recorder.record(self._section, self._model,
                                       usage.prompt_tokens, usage.completion_tokens,
                                       self._pipeline_id, duration_ms=api_ms,
                                       model_snapshot=served_model)
        return LlmCompletion(data=data, usage=usage, model=served_model)
