"""Tests for the LLM provider factory (provider seam) — no DB, no API."""
import pytest

from finiexragengine.core.llm.openai_provider import OpenAIProvider
from finiexragengine.core.llm.provider_factory import build_provider
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import LlmConfig


def test_openai_provider_resolves():
    provider = build_provider(LlmConfig(provider='openai'), 'gpt-4o-mini')
    assert isinstance(provider, OpenAIProvider)


def test_unknown_provider_fails_loudly():
    # Fail at assembly, before any call — a typo'd provider never reaches an API.
    with pytest.raises(ConfigurationError) as exc:
        build_provider(LlmConfig(provider='anthropic'), 'some-model')
    assert 'anthropic' in str(exc.value)
