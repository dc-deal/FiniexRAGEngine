"""Smoke tests for the bootable API shell (scaffold)."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.config_types.pipeline_config_types import TriggerConfig
from finiexragengine.types.worker_types import WorkerState


def _loaded_registry() -> PipelineRegistry:
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    return registry


def _configured_symbols(pipeline_id: str) -> set:
    """The pipeline's symbols as the app actually resolves them (base + any user override)."""
    return set(_loaded_registry().get(pipeline_id).get_config().symbol_keys())


def _run_client() -> TestClient:
    """A client for the two `/run` cases, with the route explicitly registered.

    `create_app` leaves `POST /run` unregistered (ISSUE_98) — an external request must not be able
    to cause spend. These two tests are about the *router's* behaviour (the envelope contract, and
    the 404 for an unknown pipeline), so they ask for the route rather than relying on the app's
    default. Without this they would still have passed: both expect a 404 somewhere, and an absent
    route supplies one for the wrong reason. Scaffold-mock mode makes the run free.
    """
    from fastapi import FastAPI

    from finiexragengine.api.endpoints.sentiment_router import build_sentiment_router

    app = FastAPI()
    app.include_router(build_sentiment_router(_loaded_registry(), run_enabled=True))
    return TestClient(app)


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


def test_run_returns_envelope_for_all_symbols() -> None:
    response = _run_client().post('/v1/pipelines/crypto_sentiment/run')
    assert response.status_code == 200
    body = response.json()
    assert body['pipeline_id'] == 'crypto_sentiment'
    assert body['outcome_type'] == 'sentiment_fear_greed'
    # Contract: exactly the configured symbols are present (robust to a user override that
    # narrows the set — the invariant is completeness, not a fixed count).
    assert {row['symbol'] for row in body['result']} == _configured_symbols('crypto_sentiment')


def test_unknown_pipeline_returns_404() -> None:
    response = _run_client().post('/v1/pipelines/does_not_exist/run')
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


def test_an_unparseable_timeframe_never_reaches_the_listing() -> None:
    """The listing cannot be asked about a bad timeframe, because the config refuses one first.

    This used to be the listing's own problem: it converted `trigger.timeframe` itself and reported
    an unparseable one as absent, so the endpoint stayed answerable while the misconfiguration
    surfaced later at the supervisor. That converter was a second derivation of a number `/health`
    already served from `TriggerConfig.cadence_seconds`, so it is gone (ISSUE_9) — and the guarantee
    it approximated is now the stronger one it should always have been: an unknown frame fails at
    load, where every entry point meets it, rather than degrading at one read.
    """
    assert TriggerConfig(timeframe='M10').cadence_seconds == 600
    assert TriggerConfig(interval_seconds=300).cadence_seconds == 300     # no frame -> raw interval
    with pytest.raises(ValidationError):
        TriggerConfig(timeframe='M7')


def test_health_reports_no_journal_id_without_a_store(client: TestClient) -> None:
    """Scaffold-mock mode has no store, so there is no journal to identify (ISSUE_9).

    Absent rather than a placeholder: the field answers "which series is this", and an instance that
    writes nowhere is not a series a certificate can be taken against.
    """
    assert client.get('/v1/health').json()['journal_id'] is None


def test_environment_is_resolved_from_the_journal_never_declared(client: TestClient) -> None:
    """The name is keyed on the journal's fingerprint, so it cannot travel to another database.

    A free-standing `environment: 'production'` would claim something about the *process*: carry the
    config to another machine and it still says production. Keyed on the identity, a boot against a
    different journal simply misses the lookup and reports `unknown` — the misconfiguration
    announces itself instead of being inherited, which is what a release certificate needs.
    """
    body = client.get('/v1/health').json()
    # Scaffold-mock mode: no store, so no fingerprint, so nothing to resolve.
    assert body['journal_id'] is None
    assert body['environment'] == 'unknown'


def test_an_unmapped_journal_reports_unknown_not_a_guess() -> None:
    """The tracked example is inert: it shows the shape and can never resolve anything.

    `EXAMPLE_ID` is not twelve lowercase hex characters, so no real fingerprint can equal it. That
    is what lets the example ship in tracked config without handing every fork a name for a database
    it does not have — which would re-introduce, through the config file, exactly the failure that
    keying on the fingerprint prevents.
    """
    from finiexragengine.types.config_types.app_config_types import AppConfig
    config = AppConfig()
    assert list(config.journal_names) == ['EXAMPLE_ID']
    assert config.journal_names.get('9c3fa4c80d95', 'unknown') == 'unknown'
    named = AppConfig(journal_names={'9c3fa4c80d95': 'dev'})
    assert named.journal_names.get('9c3fa4c80d95', 'unknown') == 'dev'
    assert named.journal_names.get('other', 'unknown') == 'unknown'


def test_health_says_degraded_when_a_worker_died() -> None:
    """`status` is a claim about the engine, not a constant.

    It was a hardcoded `'ok'` until 2026-08-22 — including throughout the 37 hours an ingest worker
    lay dead on 2026-08-20. An external check polling this endpoint is exactly the thing that should
    have caught that, and it would have seen green the whole time.
    """
    from fastapi import FastAPI

    from finiexragengine.api.endpoints.health_router import build_health_router

    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()

    healthy = WorkerState(name='ingest:forex_news', kind='ingest', interval_seconds=15)
    dead = WorkerState(name='ingest:crypto_news', kind='ingest', interval_seconds=15)

    class _Supervisor:
        """Only the one method /health calls — the seam, not the whole supervisor."""

        def states(self) -> list:
            return [healthy, dead]

    def _health_body() -> dict:
        app = FastAPI()
        app.include_router(build_health_router(manager, supervisor=_Supervisor()))
        return TestClient(app).get('/v1/health').json()

    assert _health_body()['status'] == 'ok'                 # both workers fine

    # The worker's task ends: exactly what the supervisor's done-callback records.
    dead.stopped_at = datetime(2026, 8, 20, 19, 24, 56, tzinfo=timezone.utc)
    dead.stopped_reason = "NameError: name '_format_age' is not defined"

    body = _health_body()
    assert body['status'] == 'degraded'
    frozen = next(w for w in body['workers'] if w['name'] == 'ingest:crypto_news')
    assert frozen['stopped_reason'].startswith('NameError')
    assert frozen['stopped_at'] is not None
    # And the healthy one is untouched — 'degraded' names the engine, the fields name the worker.
    assert next(w for w in body['workers']
                if w['name'] == 'ingest:forex_news')['stopped_at'] is None
