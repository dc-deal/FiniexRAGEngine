"""Smoke tests for the bootable API shell (scaffold)."""
from fastapi.testclient import TestClient

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry


def _configured_symbols(pipeline_id: str) -> set:
    """The pipeline's symbols as the app actually resolves them (base + any user override)."""
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    return set(registry.get(pipeline_id).get_config().symbol_keys())


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

def test_health_reports_the_pass_deadline(client: TestClient) -> None:
    """Engine-level, next to `version` — the bound a consumer's RC-4 tolerance is derived from.

    Deliberately not repeated under each pipeline: it is one number for every worker, and a
    per-pipeline copy would claim to be a per-stream property that nobody honours.
    """
    body = client.get('/v1/health').json()
    assert body['pass_timeout_seconds'] > 0
    pipelines = client.get('/v1/pipelines').json()['pipelines']
    assert all('pass_timeout_seconds' not in p for p in pipelines)


def test_pipelines_report_the_cadence_in_seconds(client: TestClient) -> None:
    """Seconds, not the `M10` token (ISSUE_9).

    A consumer computes a staleness threshold with the number; the token is a rendering of it, and
    shipping both would leave one unread. Exposed at all because their threshold is *derived* from
    the cadence — without it, the number that blocks their order entry is a hand-copied constant.
    """
    pipelines = {p['pipeline_id']: p for p in client.get('/v1/pipelines').json()['pipelines']}
    crypto = pipelines['crypto_sentiment']
    assert crypto['cadence_seconds'] == 600          # M10
    assert 'M10' not in str(crypto.values())          # the token stays out of the response


def test_an_unparseable_timeframe_reports_as_absent_not_as_an_error(monkeypatch) -> None:
    """The listing must stay answerable when one pipeline is misconfigured.

    Raising here would make a bad timeframe surface at the wrong place — a listing endpoint failing
    for the whole registry, rather than the supervisor refusing to schedule that one worker.
    """
    from finiexragengine.api.endpoints import health_router
    assert health_router._cadence_seconds('M10') == 600
    assert health_router._cadence_seconds(None) is None
    assert health_router._cadence_seconds('M7') is None
