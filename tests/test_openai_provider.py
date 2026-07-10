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
               duration_ms=None):
        self.calls.append((section, model, prompt_tokens, completion_tokens, pipeline_id,
                           duration_ms))
        return 0.0


def _provider(completions, recorder=None):
    return OpenAIProvider(LlmConfig(), client=_Client(completions), cost_recorder=recorder)


def test_returns_parsed_data_and_usage():
    result = _provider(_Completions(content=json.dumps({'signal': 'BUY', 'confidence': 0.8}))) \
        .complete_structured('prompt', {'type': 'object'})
    assert result.data == {'signal': 'BUY', 'confidence': 0.8}
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 18


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
    section, model, prompt_tokens, completion_tokens, pipeline_id, duration_ms = recorder.calls[0]
    assert (section, model, prompt_tokens, completion_tokens, pipeline_id) == (
        'llm_eval', 'gpt-4o-mini', 11, 7, None)
    assert duration_ms is not None and duration_ms >= 0.0       # latency sample (ISSUE_32)


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
