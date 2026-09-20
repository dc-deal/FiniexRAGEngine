"""Tests for OpenAIProvider.complete_structured (ISSUE_6) — fake client, no API budget."""
import json

import httpx
import pytest
from openai import APITimeoutError, OpenAIError

from finiexragengine.core.llm.openai_provider import OpenAIProvider
from finiexragengine.exceptions.ragengine_errors import (
    LLMApiError,
    LLMParseError,
    LLMTimeoutError,
)
from finiexragengine.types.config_types.app_config_types import LlmConfig


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Response:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage(11, 7)
        self.model = 'gpt-4o-mini-2024-07-18'   # the served snapshot behind the alias


class _Completions:
    def __init__(self, content=None, exc=None):
        self._content = content
        self._exc = exc
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        return _Response(self._content)


class _Client:
    def __init__(self, completions):
        self.chat = type('Chat', (), {'completions': completions})()


class _RecRecorder:
    def __init__(self):
        self.calls = []

    def record(self, section, model, prompt_tokens, completion_tokens=0, pipeline_id=None,
               duration_ms=None, model_snapshot=None):
        self.calls.append((section, model, prompt_tokens, completion_tokens, pipeline_id,
                           duration_ms, model_snapshot))
        return 0.0


def _provider(completions, recorder=None):
    # The model is an explicit argument now — it comes from the pipeline's declared
    # llm.model, never from a global default.
    return OpenAIProvider(LlmConfig(), 'gpt-4o-mini', client=_Client(completions),
                          cost_recorder=recorder)


def test_temperature_is_sent_to_a_model_that_accepts_it():
    completions = _Completions(content=json.dumps({'signal': 'BUY'}))
    _provider(completions).complete_structured('prompt', {'type': 'object'})

    assert completions.kwargs['temperature'] == LlmConfig().temperature


def test_temperature_is_omitted_for_a_family_that_rejects_it():
    """`gpt-5-*` has no temperature knob — sending it is a hard 400, not a clamped value.

    Observed live on 2026-09-20: the first pass of the nano variant failed on all nine symbols
    with `Unsupported value: 'temperature' does not support 0.1 with this model`. The parameter
    must not be in the payload at all; a different value would not help.
    """
    completions = _Completions(content=json.dumps({'signal': 'BUY'}))
    OpenAIProvider(LlmConfig(), 'gpt-5-nano', client=_Client(completions)) \
        .complete_structured('prompt', {'type': 'object'})

    assert 'temperature' not in completions.kwargs
    assert completions.kwargs['model'] == 'gpt-5-nano'


def test_a_dated_snapshot_of_that_family_is_also_recognised():
    """The prefix is the rule, so `gpt-5-nano-2026-…` must not reintroduce the 400."""
    completions = _Completions(content=json.dumps({'signal': 'BUY'}))
    OpenAIProvider(LlmConfig(), 'gpt-5-nano-2026-08-07', client=_Client(completions)) \
        .complete_structured('prompt', {'type': 'object'})

    assert 'temperature' not in completions.kwargs


def test_the_omission_is_announced_once_and_not_per_call(caplog):
    """A configured value that never reached the API is stated — but not in every log row.

    `llm.temperature` is inside the config fingerprint, so an envelope from this stream carries
    a temperature that did not apply. Silent would make the fingerprint a claim nobody checks;
    once per call would bury it.
    """
    completions = _Completions(content=json.dumps({'signal': 'BUY'}))
    provider = OpenAIProvider(LlmConfig(), 'gpt-5-nano', client=_Client(completions))
    with caplog.at_level('INFO'):
        provider.complete_structured('prompt', {'type': 'object'})
        provider.complete_structured('prompt', {'type': 'object'})

    said = [r for r in caplog.records if 'temperature omitted' in r.getMessage()]
    assert len(said) == 1
    assert 'gpt-5-nano' in said[0].getMessage()


def test_returns_parsed_data_and_usage():
    result = _provider(_Completions(content=json.dumps({'signal': 'BUY', 'confidence': 0.8}))) \
        .complete_structured('prompt', {'type': 'object'})
    assert result.data == {'signal': 'BUY', 'confidence': 0.8}
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 18
    assert result.model == 'gpt-4o-mini-2024-07-18'   # served snapshot captured


def test_passes_response_format_and_config():
    completions = _Completions(content='{}')
    _provider(completions).complete_structured('p', {'type': 'object'})
    assert completions.kwargs['model'] == 'gpt-4o-mini'
    assert completions.kwargs['temperature'] == 0.1
    assert completions.kwargs['response_format']['type'] == 'json_schema'


def test_records_cost_when_recorder_set():
    recorder = _RecRecorder()
    _provider(_Completions(content='{}'), recorder).complete_structured('p', {})
    assert len(recorder.calls) == 1
    (section, model, prompt_tokens, completion_tokens,
     pipeline_id, duration_ms, model_snapshot) = recorder.calls[0]
    assert (section, model, prompt_tokens, completion_tokens, pipeline_id) == (
        'llm_eval', 'gpt-4o-mini', 11, 7, None)
    assert duration_ms is not None and duration_ms >= 0.0       # latency sample (ISSUE_32)
    assert model_snapshot == 'gpt-4o-mini-2024-07-18'           # served-model trace


def test_bad_json_raises_parse_error():
    with pytest.raises(LLMParseError):
        _provider(_Completions(content='not json')).complete_structured('p', {})


def test_timeout_maps_to_llm_timeout_error():
    exc = APITimeoutError(request=httpx.Request('POST', 'https://api.openai.com/v1/chat'))
    with pytest.raises(LLMTimeoutError):
        _provider(_Completions(exc=exc)).complete_structured('p', {})


def test_backend_error_maps_to_llm_api_error():
    with pytest.raises(LLMApiError):
        _provider(_Completions(exc=OpenAIError('boom'))).complete_structured('p', {})
