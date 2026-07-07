"""Smoke tests for the bootable API shell (scaffold)."""
from fastapi.testclient import TestClient


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
    assert len(body['result']) == 8  # all configured symbols returned


def test_unknown_pipeline_returns_404(client: TestClient) -> None:
    response = client.post('/v1/pipelines/does_not_exist/run')
    assert response.status_code == 404
