"""OpenAICallRunner — our retry loop in place of the SDK's (ISSUE_128). No API, no waiting.

Every case drives the runner with a scripted call that raises the SDK's real exception classes, a
recording sink for the failed attempts, and a sleep that only records how long it was asked to wait.
"""
import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)

from finiexragengine.core.llm.openai_call_runner import OpenAICallRunner
from finiexragengine.core.pipeline.pass_executor import (
    build_pass_executor,
    pass_seconds_remaining,
    run_pass,
)

_REQUEST = httpx.Request('POST', 'https://api.openai.test/v1/chat/completions')


def _status(status: int, code: Optional[str] = None,
            headers: Optional[Dict[str, str]] = None) -> APIStatusError:
    """An SDK status error as the client raises it: the response, its headers, the error code."""
    response = httpx.Response(status, request=_REQUEST, headers=headers or {})
    body = {'code': code} if code else None
    cls = {400: BadRequestError, 429: RateLimitError}.get(
        status, InternalServerError if status >= 500 else APIStatusError)
    return cls(f'HTTP {status}', response=response, body=body)


def _timeout() -> APITimeoutError:
    return APITimeoutError(request=_REQUEST)


class _Script:
    """A call that raises the scripted errors in order, then answers; records each timeout."""

    def __init__(self, *errors: Exception) -> None:
        self._errors = list(errors)
        self.timeouts: List[float] = []

    def __call__(self, timeout: float) -> str:
        self.timeouts.append(timeout)
        if self._errors:
            raise self._errors.pop(0)
        return 'answer'


class _Harness:
    def __init__(self, max_retries: int = 2, timeout: float = 30.0,
                 jitter: float = 0.0) -> None:
        self.failed: List[Tuple[int, str, float]] = []
        self.slept: List[float] = []
        self.runner = OpenAICallRunner(
            '[LLM] test · gpt-test', max_retries, timeout,
            on_failed_attempt=lambda attempt, outcome, ms: self.failed.append(
                (attempt, outcome, ms)),
            sleep=self.slept.append, jitter=lambda: jitter)


# --- the happy paths ------------------------------------------------------------------------

def test_an_answer_on_the_first_attempt_records_nothing_failed():
    harness = _Harness()
    answered = harness.runner.run(_Script())
    assert (answered.value, answered.attempt) == ('answer', 1)
    assert answered.duration_ms >= 0.0
    assert harness.failed == [] and harness.slept == []


def test_a_timeout_then_an_answer_is_two_attempts_and_one_failed_row():
    # The 109.5 s pass this issue started from: one attempt ran out, the next answered — and only
    # the second used to be visible.
    harness = _Harness()
    answered = harness.runner.run(_Script(_timeout()))
    assert answered.attempt == 2
    assert [(attempt, outcome) for attempt, outcome, _ in harness.failed] == [(1, 'timeout')]
    assert harness.slept == [0.5]                    # the SDK's first backoff step, jitter off


# --- classification -------------------------------------------------------------------------

@pytest.mark.parametrize('error, outcome, retried', [
    (_timeout(), 'timeout', True),
    (APIConnectionError(request=_REQUEST), 'connection', True),
    (_status(429, 'rate_limit_exceeded'), 'rate_limited', True),
    (_status(500), 'server_error', True),
    (_status(503), 'server_error', True),
    (_status(408), 'timeout', True),                  # the SDK's "request timeout"
    (_status(409), 'timeout', True),                  # the SDK's "lock timeout"
    (_status(429, 'insufficient_quota'), 'quota', False),
    (_status(400), 'rejected', False),                # e.g. a rejected `temperature`
    (_status(401), 'rejected', False),
    (OpenAIError('no status at all'), 'rejected', False),
])
def test_each_failure_is_classified_and_only_the_transient_ones_retried(error, outcome, retried):
    harness = _Harness()
    script = _Script(error)
    if retried:
        assert harness.runner.run(script).attempt == 2
    else:
        with pytest.raises(type(error)):
            harness.runner.run(script)
        assert len(script.timeouts) == 1             # exactly one request went out
    assert harness.failed[0][:2] == (1, outcome)


def test_an_exhausted_quota_is_never_retried_and_the_original_error_surfaces():
    # The SDK retried `insufficient_quota` twice before the budget breaker heard of it.
    harness = _Harness()
    error = _status(429, 'insufficient_quota')
    with pytest.raises(RateLimitError) as raised:
        harness.runner.run(_Script(error, error, error))
    assert raised.value is error                      # unchanged, for the caller's taxonomy
    assert harness.slept == []


def test_attempts_are_capped_and_every_one_is_reported():
    harness = _Harness(max_retries=2)
    with pytest.raises(APITimeoutError):
        harness.runner.run(_Script(_timeout(), _timeout(), _timeout(), _timeout()))
    assert [(attempt, outcome) for attempt, outcome, _ in harness.failed] == [
        (1, 'timeout'), (2, 'timeout'), (3, 'timeout')]
    assert harness.slept == [0.5, 1.0]


def test_max_retries_zero_is_a_single_attempt():
    harness = _Harness(max_retries=0)
    with pytest.raises(APITimeoutError):
        harness.runner.run(_Script(_timeout()))
    assert len(harness.failed) == 1 and harness.slept == []


# --- the SDK's policy, mirrored -------------------------------------------------------------

def test_backoff_doubles_from_half_a_second_and_stops_at_eight():
    harness = _Harness(max_retries=6)
    harness.runner.run(_Script(*[_status(500) for _ in range(6)]))
    assert harness.slept == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]


def test_jitter_shortens_the_wait_by_at_most_a_quarter():
    harness = _Harness(jitter=1.0)                    # random() at its ceiling
    harness.runner.run(_Script(_status(500), _status(500)))
    assert harness.slept == [0.375, 0.75]


@pytest.mark.parametrize('headers, wait', [
    ({'retry-after-ms': '2500'}, 2.5),               # the precise header wins
    ({'retry-after': '3'}, 3.0),
    ({'retry-after-ms': '2500', 'retry-after': '9'}, 2.5),
])
def test_a_server_directed_wait_is_honoured(headers, wait):
    harness = _Harness()
    harness.runner.run(_Script(_status(429, 'rate_limit_exceeded', headers)))
    assert harness.slept == [wait]


def test_a_retry_after_beyond_two_minutes_is_not_waited_out():
    harness = _Harness()
    with pytest.raises(RateLimitError):
        harness.runner.run(_Script(_status(429, 'rate_limit_exceeded', {'retry-after': '300'})))
    assert harness.slept == []


def test_x_should_retry_overrides_the_status():
    harness = _Harness()
    with pytest.raises(InternalServerError):
        harness.runner.run(_Script(_status(500, headers={'x-should-retry': 'false'})))
    assert harness.slept == []
    assert _Harness().runner.run(
        _Script(_status(400, headers={'x-should-retry': 'true'}))).attempt == 2


def test_x_should_retry_cannot_retry_an_exhausted_quota():
    harness = _Harness()
    with pytest.raises(RateLimitError):
        harness.runner.run(_Script(_status(429, 'insufficient_quota',
                                           {'x-should-retry': 'true'})))


# --- the pass deadline ----------------------------------------------------------------------

def _inside_a_pass(seconds: float, harness: _Harness, script: _Script) -> object:
    """Run the call where the workers run it: inside `run_pass`, under its deadline."""
    executor = build_pass_executor(1)

    async def _run() -> object:
        return await run_pass(executor, seconds, harness.runner.run, script)
    try:
        return asyncio.run(_run())
    finally:
        executor.shutdown(wait=False)


def test_outside_a_pass_there_is_no_deadline_and_the_timeout_stands():
    script = _Script()
    _Harness(timeout=120.0).runner.run(script)
    assert pass_seconds_remaining() is None
    assert script.timeouts == [120.0]


def test_an_attempt_never_gets_more_time_than_the_pass_has_left():
    script = _Script()
    _inside_a_pass(30.0, _Harness(timeout=120.0), script)
    assert 25.0 < script.timeouts[0] <= 30.0


def test_no_retry_starts_that_could_not_finish_before_the_deadline():
    # 5.2 s left: the 0.5 s backoff plus the 5 s floor do not fit, so the error surfaces now
    # instead of a paid attempt the abandoned pass would never read.
    harness = _Harness()
    script = _Script(_timeout())
    with pytest.raises(APITimeoutError):
        _inside_a_pass(5.2, harness, script)
    assert len(script.timeouts) == 1 and harness.slept == []
    assert harness.failed[0][:2] == (1, 'timeout')


# --- reporting ------------------------------------------------------------------------------

def test_a_broken_sink_never_masks_the_call_error(caplog):
    def _broken(attempt: int, outcome: str, ms: float) -> None:
        raise RuntimeError('cost log down')

    runner = OpenAICallRunner('[LLM] test · gpt-test', 0, 30.0, on_failed_attempt=_broken)
    with caplog.at_level(logging.WARNING), pytest.raises(BadRequestError):
        runner.run(_Script(_status(400)))
    assert any('could not be recorded' in record.getMessage() for record in caplog.records)


def test_each_retry_is_one_warning_line(caplog):
    harness = _Harness()
    with caplog.at_level(logging.WARNING):
        harness.runner.run(_Script(_status(503)))
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert lines[0].startswith('[LLM] test · gpt-test · attempt 1/3 server_error (503) after ')
    assert lines[0].endswith('— retrying in 0.5 s')


def test_a_first_attempt_rejection_is_left_to_the_caller_to_report(caplog):
    # The provider maps it into its taxonomy and the run logs it; a second line here would be noise.
    with caplog.at_level(logging.WARNING), pytest.raises(BadRequestError):
        _Harness().runner.run(_Script(_status(400)))
    assert caplog.records == []
