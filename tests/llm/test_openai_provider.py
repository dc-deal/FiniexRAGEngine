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
from finiexragengine.types.outcome_types import SentimentLlmOutput


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
        self.attempts = []

    def record(self, section, model, prompt_tokens, completion_tokens=0, pipeline_id=None,
               duration_ms=None, model_snapshot=None, attempt=None, outcome='ok'):
        self.calls.append((section, model, prompt_tokens, completion_tokens, pipeline_id,
                           duration_ms, model_snapshot))
        self.attempts.append((attempt, outcome))
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


def test_timeout_maps_to_llm_timeout_error(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    exc = APITimeoutError(request=httpx.Request('POST', 'https://api.openai.com/v1/chat'))
    with pytest.raises(LLMTimeoutError):
        _provider(_Completions(exc=exc)).complete_structured('p', {})


def test_backend_error_maps_to_llm_api_error():
    with pytest.raises(LLMApiError):
        _provider(_Completions(exc=OpenAIError('boom'))).complete_structured('p', {})


# --- strict structured output, by model profile ------------------------------------------

def _sent_format(model: str) -> dict:
    completions = _Completions(content='{}')
    OpenAIProvider(LlmConfig(), model, client=_Client(completions)).complete_structured(
        'p', SentimentLlmOutput.model_json_schema())
    return completions.kwargs['response_format']['json_schema']


def test_the_reasoning_family_is_called_with_strict_output():
    # The 2026-10-11 dev pass: gpt-5-mini returned `urgingency` for `urgency` with the schema as a
    # hint, so both rows degraded. Strict decoding cannot produce a key the schema does not name.
    sent = _sent_format('gpt-5-mini')
    assert sent['strict'] is True
    schema = sent['schema']
    assert set(schema['required']) == set(schema['properties'])   # every field listed …
    assert 'breaking_reason' in schema['required']
    assert 'default' not in schema['properties']['breaking_reason']
    assert {'type': 'null'} in schema['properties']['breaking_reason']['anyOf']  # … still nullable
    assert schema['properties']['urgency']['maximum'] == 1.0       # ranges kept


def test_the_live_series_family_is_called_exactly_as_before():
    # gpt-4o-mini produces the public series: the schema goes out untouched and non-strict.
    sent = _sent_format('gpt-4o-mini')
    assert sent['strict'] is False
    assert sent['schema'] == SentimentLlmOutput.model_json_schema()


def test_a_fine_tune_is_called_like_its_base_model():
    # `ft:gpt-5-…` used to get `temperature` (a hard 400), because the prefix check saw `ft:`.
    completions = _Completions(content='{}')
    OpenAIProvider(LlmConfig(), 'ft:gpt-5-mini:org:custom:abc',
                   client=_Client(completions)).complete_structured('p', {'type': 'object'})
    assert 'temperature' not in completions.kwargs
    assert completions.kwargs['response_format']['json_schema']['strict'] is True


def test_the_strict_schema_is_a_copy():
    schema = SentimentLlmOutput.model_json_schema()
    before = json.dumps(schema, sort_keys=True)
    _sent_format('gpt-5')
    assert json.dumps(SentimentLlmOutput.model_json_schema(), sort_keys=True) == before



# --- retries are ours, every attempt recorded (ISSUE_128) ------------------------------------

class _Flaky(_Completions):
    """Raises the given errors in order, then answers; counts the requests."""

    def __init__(self, content, *errors):
        super().__init__(content=content)
        self._errors = list(errors)
        self.requests = 0

    def create(self, **kwargs):
        self.requests += 1
        self.kwargs = kwargs
        if self._errors:
            raise self._errors.pop(0)
        return _Response(self._content)


def _timed_out():
    return APITimeoutError(request=httpx.Request('POST', 'https://api.openai.com/v1/chat'))


def test_the_client_is_built_without_sdk_retries():
    # The SDK's own loop retried twice out of sight; the runner does it now, recording each try.
    provider = OpenAIProvider(LlmConfig(), 'gpt-4o-mini', api_key='test-key')
    assert provider._get_client().max_retries == 0


@pytest.mark.parametrize('model, timeout', [('gpt-4o-mini', 30.0), ('gpt-5-mini', 120.0)])
def test_each_family_is_called_with_its_own_timeout(model, timeout):
    completions = _Completions(content='{}')
    OpenAIProvider(LlmConfig(), model, client=_Client(completions)).complete_structured('p', {})
    assert completions.kwargs['timeout'] == timeout


def test_a_timed_out_attempt_is_recorded_and_the_retry_answers(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    recorder = _RecRecorder()
    completions = _Flaky('{"signal": "BUY"}', _timed_out())
    result = _provider(completions, recorder).complete_structured('p', {})
    assert result.data == {'signal': 'BUY'}
    assert completions.requests == 2
    assert recorder.attempts == [(1, 'timeout'), (2, 'ok')]
    assert recorder.calls[0][2:4] == (None, None)        # unknown spend, not a plausible 0
    assert recorder.calls[1][2:4] == (11, 7)


def test_a_quota_stop_is_one_request_not_three(monkeypatch):
    from openai import RateLimitError
    monkeypatch.setattr('time.sleep', lambda seconds: pytest.fail('a quota stop was retried'))
    quota = RateLimitError('quota', response=httpx.Response(
        429, request=httpx.Request('POST', 'https://api.openai.com/v1/chat')),
        body={'code': 'insufficient_quota'})
    completions = _Flaky('{}', quota, quota, quota)
    with pytest.raises(LLMApiError):                     # no breaker wired here — the taxonomy
        _provider(completions).complete_structured('p', {})
    assert completions.requests == 1


def test_a_non_json_completion_is_recorded_before_it_raises():
    # It was billed. Before ISSUE_128 the parse failure raised ahead of the record.
    recorder = _RecRecorder()
    with pytest.raises(LLMParseError):
        _provider(_Completions(content='not json'), recorder).complete_structured('p', {})
    assert recorder.attempts == [(1, 'ok')]
