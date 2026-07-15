"""Cost circuit-breaker (ISSUE_47) — guard lifecycle + the paid-seam reaction.

Pure logic + fake OpenAI clients: no DB, no network, no API budget. The guard reacts to the
provider's quota signal (429 insufficient_quota), suspends, backs off, re-probes, auto-resumes.
"""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from openai import OpenAIError

from finiexragengine.core.llm.openai_provider import OpenAIProvider
from finiexragengine.core.llm.openai_quota import is_quota_exceeded
from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.rag.openai_embedder import OpenAIEmbedder
from finiexragengine.exceptions.ragengine_errors import (
    BudgetExceededError,
    EmbeddingError,
    LLMApiError,
)
from finiexragengine.types.config_types.app_config_types import (
    CircuitBreakerConfig,
    EmbeddingConfig,
    LlmConfig,
)


def _guard(**kw):
    return BudgetGuard(CircuitBreakerConfig(**kw))


# --- the quota discriminator (quota vs a transient rate-limit) ------------------------

class _QuotaError(OpenAIError):
    code = 'insufficient_quota'


class _RateError(OpenAIError):
    code = 'rate_limit_exceeded'


class _OtherBilling(OpenAIError):
    code = 'billing_hard_limit_reached'
    status_code = 429


def test_is_quota_exceeded_only_for_insufficient_quota():
    assert is_quota_exceeded(_QuotaError('quota')) is True
    assert is_quota_exceeded(_RateError('slow down')) is False
    assert is_quota_exceeded(ValueError('unrelated')) is False


def test_is_quota_exceeded_robust_to_other_budget_signals():
    # A spend limit under a *different* code (or bare 429) is still a budget stop — not a
    # rate-limit — so the breaker engages regardless of the exact code (ISSUE_47 hardening).
    assert is_quota_exceeded(_OtherBilling('spend limit reached')) is True
    assert is_quota_exceeded(ValueError('billing hard limit reached')) is True   # message fallback


# --- guard lifecycle: suspend -> back off -> single probe -> resume -------------------

def test_fresh_guard_always_attempts():
    assert _guard().should_attempt() is True


def test_quota_suspends_and_gates_during_cooloff():
    g = _guard(reprobe_interval_seconds=600)
    g.on_quota_error()
    assert g.suspended is True
    assert g.should_attempt() is False               # within the cool-off → skip paid work


def test_cooloff_elapsed_allows_exactly_one_probe():
    g = _guard(reprobe_interval_seconds=600)
    g.on_quota_error()
    g._retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)   # cool-off has elapsed
    assert g.should_attempt() is True                # the one probe is allowed
    assert g.should_attempt() is False               # …and the window re-armed → the rest wait


def test_success_clears_the_suspend_auto_resume():
    g = _guard(reprobe_interval_seconds=600)
    g.on_quota_error()
    g.record_spend(0.002)                            # a call succeeded → quota is back
    assert g.suspended is False
    assert g.should_attempt() is True


def test_disabled_guard_never_suspends():
    g = _guard(enabled=False, reprobe_interval_seconds=600)
    g.on_quota_error()
    assert g.should_attempt() is True                # master switch off → no gating


def test_soft_daily_warns_once_without_suspending(caplog):
    g = _guard(soft_daily_usd=1.0)
    with caplog.at_level(logging.WARNING):
        g.record_spend(0.6)
        g.record_spend(0.6)                          # crosses $1.00 here
        g.record_spend(0.6)                          # already warned → silent
    assert sum('soft daily' in r.message for r in caplog.records) == 1
    assert g.suspended is False                      # a soft line only warns, never suspends


def test_status_shape_for_health():
    g = _guard(reprobe_interval_seconds=600)
    # The provider passes its own error code through — the guard only echoes it (it must not
    # know any vendor's vocabulary itself), and /health reports what actually came back.
    g.on_quota_error(reason='insufficient_quota')
    status = g.status()
    assert status['suspended'] is True and status['reason'] == 'insufficient_quota'
    assert status['retry_at'] is not None


def test_guard_default_reason_is_vendor_neutral():
    g = _guard(reprobe_interval_seconds=600)
    g.on_quota_error()                               # no code supplied (a provider that has none)
    assert g.status()['reason'] == 'quota'           # never a hard-coded OpenAI code


# --- the paid seams react (fake OpenAI clients) --------------------------------------

class _Raises:
    def __init__(self, exc):
        self._exc = exc

    def create(self, **_kw):
        raise self._exc


def _llm_client(exc):
    return SimpleNamespace(chat=SimpleNamespace(completions=_Raises(exc)))


def test_provider_quota_error_arms_breaker_and_raises_budget():
    guard = _guard(reprobe_interval_seconds=600)
    provider = OpenAIProvider(LlmConfig(), 'gpt-4o-mini',
                              client=_llm_client(_QuotaError('quota')), budget_guard=guard)
    with pytest.raises(BudgetExceededError):
        provider.complete_structured('p', {})
    assert guard.suspended is True                   # the quota signal armed the breaker


def test_provider_rate_limit_stays_llm_api_error():
    guard = _guard(reprobe_interval_seconds=600)
    provider = OpenAIProvider(LlmConfig(), 'gpt-4o-mini',
                              client=_llm_client(_RateError('slow')), budget_guard=guard)
    with pytest.raises(LLMApiError):                 # a transient throttle, NOT a budget stop
        provider.complete_structured('p', {})
    assert guard.suspended is False                  # breaker untouched


def test_provider_gate_skips_the_call_while_suspended():
    guard = _guard(reprobe_interval_seconds=600)
    guard.on_quota_error()                            # suspended, within cool-off
    # A client whose create() would fail the test if reached — the gate must short-circuit first.
    provider = OpenAIProvider(LlmConfig(), 'gpt-4o-mini',
                              client=_llm_client(AssertionError('must not call the API')),
                              budget_guard=guard)
    with pytest.raises(BudgetExceededError):
        provider.complete_structured('p', {})


def test_embedder_quota_error_arms_breaker():
    guard = _guard(reprobe_interval_seconds=600)
    embedder = OpenAIEmbedder(EmbeddingConfig(),
                              client=SimpleNamespace(embeddings=_Raises(_QuotaError('quota'))),
                              budget_guard=guard)
    with pytest.raises(BudgetExceededError):
        embedder.embed(['text'])
    assert guard.suspended is True


def test_embedder_gate_skips_while_suspended():
    guard = _guard(reprobe_interval_seconds=600)
    guard.on_quota_error()
    embedder = OpenAIEmbedder(EmbeddingConfig(),
                              client=SimpleNamespace(embeddings=_Raises(AssertionError('no call'))),
                              budget_guard=guard)
    with pytest.raises(BudgetExceededError):
        embedder.embed(['text'])
