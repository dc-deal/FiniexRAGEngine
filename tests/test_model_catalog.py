"""Tests for the model catalog / staged startup model check (ISSUE_40) — no API, no DB."""
from types import SimpleNamespace

from openai import OpenAIError

from finiexragengine.core.llm.model_catalog import (
    ModelCatalog,
    check_configured_models,
    format_model_check,
    verify_configured_models,
)
from finiexragengine.types.config_types.app_config_types import AppConfig, LlmConfig


class _Models:
    def __init__(self, ids=None, exc=None):
        self._ids = ids or []
        self._exc = exc
        self.list_calls = 0

    def list(self):
        self.list_calls += 1
        if self._exc is not None:
            raise self._exc
        return [SimpleNamespace(id=model_id) for model_id in self._ids]


class _Client:
    def __init__(self, ids=None, exc=None):
        self.models = _Models(ids, exc)


def test_check_maps_availability():
    catalog = ModelCatalog(client=_Client(
        ids=['gpt-4o-mini', 'gpt-4o-mini-2024-07-18', 'gpt-4o']))
    checked = catalog.check(['gpt-4o-mini', 'ft:gpt-4o-mini:acme::gone'])
    assert checked == {'gpt-4o-mini': True, 'ft:gpt-4o-mini:acme::gone': False}


def test_available_ids_memoized_per_catalog():
    # Several check()s (ingest + llm sections on one endpoint) share one free fetch.
    client = _Client(ids=['gpt-4o-mini'])
    catalog = ModelCatalog(client=client)
    catalog.check(['gpt-4o-mini'])
    catalog.check(['gpt-4o'])
    assert client.models.list_calls == 1


def test_sections_share_one_endpoint_by_default(monkeypatch):
    # llm.base_url unset -> both sections check against one catalog = one client.
    built = []

    def _factory(base_url=None):
        built.append(base_url)
        return _Client(ids=['text-embedding-3-small', 'gpt-4o-mini', 'gpt-4o'])

    monkeypatch.setattr('finiexragengine.core.llm.model_catalog.OpenAI', _factory)
    sections = check_configured_models(AppConfig())
    assert built == [None]
    assert sections[0] == ('ingest — embedding model', {'text-embedding-3-small': True})
    assert sections[1][1] == {'gpt-4o-mini': True, 'gpt-4o': True}


def test_embedding_checks_default_endpoint_with_custom_llm_base_url(monkeypatch):
    # A self-hosted llm endpoint (vLLM) does not serve text-embedding-* — the embedding
    # model must be checked against the OpenAI default, not falsely reported MISSING.
    def _factory(base_url=None):
        if base_url == 'http://vllm:8000/v1':
            return _Client(ids=['my-local-llm'])
        return _Client(ids=['text-embedding-3-small'])

    monkeypatch.setattr('finiexragengine.core.llm.model_catalog.OpenAI', _factory)
    config = AppConfig(llm=LlmConfig(base_url='http://vllm:8000/v1',
                                     allowed_models=['my-local-llm']))
    sections = check_configured_models(config)
    assert sections[0][1] == {'text-embedding-3-small': True}
    assert sections[1][1] == {'my-local-llm': True}


def test_verify_warns_per_missing_model(caplog, monkeypatch):
    config = AppConfig(llm=LlmConfig(allowed_models=['gpt-4o-mini', 'typo-model']))
    monkeypatch.setattr(
        'finiexragengine.core.llm.model_catalog.OpenAI',
        lambda base_url=None: _Client(ids=['gpt-4o-mini', 'text-embedding-3-small']))
    with caplog.at_level('WARNING'):
        assert verify_configured_models(config) is True
    assert any('typo-model' in record.message for record in caplog.records)
    assert not any('gpt-4o-mini' in record.message and 'not available' in record.message
                   for record in caplog.records)


def test_verify_warns_on_missing_embedding_model(caplog, monkeypatch):
    # The ingest section carries the same weight: a vanished embedding model warns too.
    monkeypatch.setattr(
        'finiexragengine.core.llm.model_catalog.OpenAI',
        lambda base_url=None: _Client(ids=['gpt-4o-mini', 'gpt-4o']))
    with caplog.at_level('WARNING'):
        assert verify_configured_models(AppConfig()) is True
    assert any('text-embedding-3-small' in record.message and 'ingest' in record.message
               for record in caplog.records)


def test_verify_is_soft_on_provider_failure(caplog, monkeypatch):
    # A transient outage (or a missing key) logs and moves on — boot is never blocked.
    monkeypatch.setattr(
        'finiexragengine.core.llm.model_catalog.OpenAI',
        lambda base_url=None: _Client(exc=OpenAIError('down')))
    with caplog.at_level('WARNING'):
        assert verify_configured_models(AppConfig()) is False
    assert any('skipped' in record.message for record in caplog.records)


def test_format_renders_staged_sections():
    sections = [
        ('ingest — embedding model', {'text-embedding-3-small': True}),
        ('llm stage — eval models (allowed_models)', {'gpt-4o-mini': True, 'gpt-4o': False}),
    ]
    used_by = {
        'text-embedding-3-small': ['shared corpus + query embedding (all pipelines)'],
        'gpt-4o-mini': ['crypto_sentiment', 'forex_macro_sentiment'],
    }
    text = format_model_check(sections, used_by, 'api.openai.com (default)')
    assert 'Model Check' in text and '---' in text        # pattern table
    assert 'ingest — embedding model' in text
    assert 'llm stage — eval models (allowed_models)' in text
    assert 'shared corpus + query embedding (all pipelines)' in text
    assert 'crypto_sentiment, forex_macro_sentiment' in text
    assert '3 models checked' in text and '1 MISSING: gpt-4o' in text
