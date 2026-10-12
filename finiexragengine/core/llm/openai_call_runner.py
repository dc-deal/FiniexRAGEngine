"""One OpenAI call as up to N recorded attempts — our retry loop in place of the SDK's (ISSUE_128).

The SDK retries on its own: 2 retries by default, on timeouts, connection errors, 408/409/429 and
5xx. Nothing in the engine set `max_retries`, so every call ran that loop — and every attempt but
the last was invisible. A "90 s" timeout was ~271 s; `cost_log` recorded one row per *successful*
call, with the hidden attempts and the backoff inside its `duration_ms`; an attempt that timed out
may have been billed and left no trace; and a quota 429 was retried twice before the budget
breaker saw it.

So the clients are built with `max_retries=0` and this unit runs the loop instead, with the SDK's
own policy (openai 3.3.1, `_base_client.py` + `_constants.py`) mirrored rather than reinvented:
retry the retryable, honour `x-should-retry`, wait `retry-after-ms` / `retry-after` up to two
minutes, otherwise back off `min(0.5 · 2ⁿ, 8 s)` with 0.75–1.0 jitter. What changes is only what we
can see: every failed attempt is reported to the caller as it happens (one `cost_log` row each, its
spend unknown), every retry is a log line, and an attempt that cannot finish before the pass
deadline is never started.

Provider-specific on purpose (it reads the OpenAI exception classes), so it sits beside the other
OpenAI units behind the provider seam; a second provider brings its own.
"""
import email.utils
import logging
import math
import random
import time
from time import perf_counter
from typing import Callable, Optional, Tuple, TypeVar

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError

from finiexragengine.core.llm.openai_quota import is_quota_exceeded
from finiexragengine.core.pipeline.pass_executor import pass_seconds_remaining
from finiexragengine.types.llm_types import AttemptOutcome, AttemptSuccess

logger = logging.getLogger(__name__)

T = TypeVar('T')

# Reports one failed attempt: (attempt number, outcome, that attempt's duration in ms).
FailedAttemptSink = Callable[[int, AttemptOutcome, float], None]

# The SDK's constants, mirrored: taking the loop into our hands must change what we can see, not
# how often or how fast we ask.
_INITIAL_DELAY_SECONDS = 0.5
_MAX_DELAY_SECONDS = 8.0
_MAX_RETRY_AFTER_SECONDS = 120.0

# No attempt starts with less than this left before the pass deadline, and none is given a shorter
# timeout: below it even a healthy call of the fast families (1–2 s, embeddings well under) would
# be cut off, so the attempt could only cost money and produce a timeout row.
_MIN_ATTEMPT_SECONDS = 5.0

_RETRYABLE = frozenset({'timeout', 'connection', 'rate_limited', 'server_error'})


def _classify(exc: OpenAIError) -> AttemptOutcome:
    """Which way one attempt failed. The quota check runs first: it is a 429 too."""
    if is_quota_exceeded(exc):
        return 'quota'
    if isinstance(exc, APITimeoutError):      # a subclass of APIConnectionError — test it first
        return 'timeout'
    if isinstance(exc, APIConnectionError):
        return 'connection'
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if status == 429:
            return 'rate_limited'
        if status in (408, 409):              # the SDK's "request timeout" and "lock timeout"
            return 'timeout'
        if status >= 500:
            return 'server_error'
    # Any other 4xx — and an error without a status at all, which the SDK does not retry either.
    return 'rejected'


def _retry_after_seconds(exc: OpenAIError) -> Optional[float]:
    """The server-directed wait, if the response carries one — parsed exactly as the SDK does."""
    response = getattr(exc, 'response', None)
    headers = getattr(response, 'headers', None)
    if headers is None:
        return None
    try:
        return float(headers.get('retry-after-ms', None)) / 1000.0
    except (TypeError, ValueError):
        pass
    retry_after = headers.get('retry-after')
    try:
        return float(retry_after)
    except (TypeError, ValueError):
        pass
    try:
        parsed = email.utils.parsedate_tz(retry_after)
        if parsed is None:
            return None
        return float(email.utils.mktime_tz(parsed) - time.time())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _should_retry(exc: OpenAIError, outcome: AttemptOutcome) -> Tuple[bool, Optional[float]]:
    """(retry at all, server-directed wait) — the SDK's `_should_retry`, plus the quota rule."""
    # Never: an exhausted quota is a budget stop for the breaker, not a throttle to wait out.
    if outcome == 'quota':
        return False, None
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None and math.isfinite(retry_after) \
            and retry_after > _MAX_RETRY_AFTER_SECONDS:
        return False, None
    response = getattr(exc, 'response', None)
    headers = getattr(response, 'headers', None)
    directive = headers.get('x-should-retry') if headers is not None else None
    if directive == 'true':
        return True, retry_after
    if directive == 'false':
        return False, None
    return outcome in _RETRYABLE, retry_after


class OpenAICallRunner:
    """Runs one logical OpenAI call as up to `max_retries + 1` attempts, each one accounted for.

    `call(timeout_seconds)` makes ONE request with that timeout and returns the SDK's response; the
    runner decides whether a failure is tried again and how long to wait first. A failed attempt
    goes to `on_failed_attempt` before anything else happens, so a later attempt — or the final
    raise — never loses it; a broken sink is logged and never replaces the original error. The
    successful attempt is the caller's to record, because only the caller can read its usage.

    On a final failure the **original** exception is re-raised, unchanged: the caller keeps mapping
    it into its own taxonomy (timeout, backend error, quota → budget breaker) exactly as before.
    One WARNING per retry; the final failure gets one too once a retry has happened — a
    first-attempt rejection is reported by the caller, which knows what it means.
    """

    def __init__(self, label: str, max_retries: int, timeout_seconds: float,
                 on_failed_attempt: Optional[FailedAttemptSink] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 jitter: Optional[Callable[[], float]] = None) -> None:
        self._label = label                  # '[LLM] <pipeline> · <model>', set by the caller
        self._attempts = max(0, max_retries) + 1
        self._timeout = timeout_seconds
        self._on_failed_attempt = on_failed_attempt
        # Injectable so a test can run the loop without waiting; resolved at call time otherwise.
        self._sleep = sleep
        self._jitter = jitter

    def run(self, call: Callable[[float], T]) -> AttemptSuccess[T]:
        for attempt in range(1, self._attempts + 1):
            timeout = self._attempt_timeout()
            started = perf_counter()
            try:
                value = call(timeout)
            except OpenAIError as exc:
                elapsed_ms = (perf_counter() - started) * 1000.0
                outcome = _classify(exc)
                self._report(attempt, outcome, elapsed_ms)
                retry, server_wait = _should_retry(exc, outcome)
                what = self._describe(attempt, outcome, exc, elapsed_ms)
                if not retry:
                    if attempt > 1:
                        logger.warning('%s — not retryable, giving up', what)
                    raise
                if attempt == self._attempts:
                    logger.warning('%s — no retries left, giving up', what)
                    raise
                delay = self._delay(attempt, server_wait)
                remaining = pass_seconds_remaining()
                if remaining is not None and remaining - delay < _MIN_ATTEMPT_SECONDS:
                    # The pass would be abandoned under the next attempt: it could only cost money.
                    logger.warning('%s — %.0f s left before the pass deadline, giving up',
                                   what, max(remaining, 0.0))
                    raise
                logger.warning('%s — retrying in %.1f s', what, delay)
                (self._sleep or time.sleep)(delay)
                continue
            return AttemptSuccess(value=value, attempt=attempt,
                                  duration_ms=(perf_counter() - started) * 1000.0)
        raise AssertionError('unreachable: the loop returns or raises')   # pragma: no cover

    def _attempt_timeout(self) -> float:
        """This attempt's ceiling: the configured one, shrunk to what the pass has left."""
        remaining = pass_seconds_remaining()
        if remaining is None:
            return self._timeout
        return min(self._timeout, max(remaining, _MIN_ATTEMPT_SECONDS))

    def _delay(self, attempt: int, server_wait: Optional[float]) -> float:
        """The wait before attempt `attempt + 1`: server-directed if sane, else backoff + jitter."""
        if server_wait is not None and math.isfinite(server_wait) \
                and 0 < server_wait <= _MAX_RETRY_AFTER_SECONDS:
            return server_wait
        backoff = min(_INITIAL_DELAY_SECONDS * pow(2.0, attempt - 1), _MAX_DELAY_SECONDS)
        return backoff * (1 - 0.25 * (self._jitter or random.random)())

    def _report(self, attempt: int, outcome: AttemptOutcome, elapsed_ms: float) -> None:
        if self._on_failed_attempt is None:
            return
        try:
            self._on_failed_attempt(attempt, outcome, elapsed_ms)
        except Exception as exc:   # noqa: BLE001 — recording must never mask the call's own error
            logger.warning('%s · attempt %d (%s) could not be recorded: %s',
                           self._label, attempt, outcome, exc)

    def _describe(self, attempt: int, outcome: AttemptOutcome, exc: OpenAIError,
                  elapsed_ms: float) -> str:
        status = getattr(exc, 'status_code', None)
        code = f' ({status})' if status is not None else ''
        return (f'{self._label} · attempt {attempt}/{self._attempts} {outcome}{code} '
                f'after {elapsed_ms / 1000.0:.1f} s')
