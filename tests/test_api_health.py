"""Smoke tests for the bootable API shell (scaffold)."""
from fastapi.testclient import TestClient

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry


def _configured_symbols(pipeline_id: str) -> set:
    """The pipeline's symbols as the app actually resolves them (base + any user override)."""
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    return set(registry.get(pipeline_id).get_config().symbols)


def test_health_ok(client: TestClient) -> None:
    response = client.get('/v1/health')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['service'] == 'FiniexRAGEngine'


def test_pipelines_lists_crypto_sentiment(client: TestClient) -> None:
    response = client.get('/v1/pipelines')
    assert response.status_code == 200
    ids = [pipeline['pipeline_id'] for pipeline in response.json()['pipelines']]
    assert 'crypto_sentiment' in ids


def test_run_returns_envelope_for_all_symbols(client: TestClient) -> None:
    response = client.post('/v1/pipelines/crypto_sentiment/run')
    assert response.status_code == 200
    body = response.json()
    assert body['pipeline_id'] == 'crypto_sentiment'
    assert body['outcome_type'] == 'sentiment_fear_greed'
    # Contract: exactly the configured symbols are present (robust to a user override that
    # narrows the set — the invariant is completeness, not a fixed count).
    assert {row['symbol'] for row in body['result']} == _configured_symbols('crypto_sentiment')


def test_unknown_pipeline_returns_404(client: TestClient) -> None:
    response = client.post('/v1/pipelines/does_not_exist/run')
    assert response.status_code == 404
