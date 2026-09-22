"""`GET /v1/dashboard/{name}` — the live console's state, for a viewer somewhere else (ISSUE_126).

Since the engine became a Windows service it has no console, and the panel moved to a separate
process. This route is the seam. Three properties are worth pinning, and each one exists because the
alternative draws a plausible screen that is wrong:

- **503 rather than an empty panel** when nothing is collecting. An engine running no workers and an
  engine where nothing is happening look identical once the zeros are on screen.
- **Its own grant surface.** The panel exposes spend, per-symbol signals and the activity feed; a
  token granted the signal streams has no business reading it, and the refusal is by omission rather
  than by a rule somebody has to remember to write.
- **The header is typed and the state is not.** The four fields a viewer needs to draw honestly are a
  contract; everything below them must stay free to change without a converter edit at either end.
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.api.api_app import _build_protected_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.ui.dashboard_snapshot import DashboardSnapshot, sample_dashboard
from finiexragengine.core.ui.engine_stats import EngineStats
from finiexragengine.types.config_types.app_config_types import ApiConfig

_HOLDER = 'dashboard-consumer-token'
_OTHER = 'signals-only-consumer-token'
_WIDE = 'wide-consumer-token'

_TOKENS = {
    'viewer': {'token': _HOLDER, 'grants': ['dashboard:engine'], 'note': 'the operator viewer'},
    'ide': {'token': _OTHER, 'grants': ['pipelines:*'], 'note': 'signal streams only'},
    'claude-dev': {'token': _WIDE, 'grants': ['*'], 'note': 'assistant'},
}


def _snapshot() -> DashboardSnapshot:
    stats = EngineStats(source_set_ids=['crypto_news'], pipeline_ids=['crypto_sentiment'])
    stats.push_event('LLM', 'success · 9 symbols')
    return sample_dashboard(stats, version='0.3.3', journal_named=True,
                            engine_started_at=datetime(2026, 9, 22, 11, 30, tzinfo=timezone.utc))


def _client(*, with_provider: bool) -> TestClient:
    """The route as `create_app` mounts it, never the router on its own.

    The two halves of access live in different places: **authentication** sits on the shared
    protected router, which is why nobody can forget it, and **authorization** is declared per
    domain router. An isolated `FastAPI()` carrying only the domain router therefore has no
    identified consumer at all — and with no consumer the grant check has nobody to refuse, so every
    call answers 200. That is a property of the test harness rather than of the route, and building
    the real composition is the only way the negative cases mean anything.

    Tokens go through `ApiConfig` rather than being passed as raw dicts: that is where a grant is
    validated against `GRANT_SURFACES`, so a typo'd surface fails here exactly as it would at boot.
    """
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    api_config = ApiConfig(tokens=_TOKENS)
    app = FastAPI()
    app.include_router(_build_protected_router(
        registry, api_config, TokenRegistry(api_config.tokens),
        dashboard_provider=_snapshot if with_provider else None))
    return TestClient(app)


def _get(client: TestClient, path: str, token: str = _HOLDER):
    return client.get(path, headers={'Authorization': f'Bearer {token}'})


def test_a_reading_carries_the_header_a_viewer_cannot_draw_without() -> None:
    """`snapshot_at`, `engine_started_at`, `version`, `journal_named` — the contract half.

    Each one exists because the viewer would otherwise substitute its own: its clock for the
    engine's, its own process start for the engine's uptime, its local config for the version, and a
    benign default for an identity nobody established.
    """
    body = _get(_client(with_provider=True), '/v1/dashboard/engine').json()

    assert body['view'] == 'engine'
    assert body['version'] == '0.3.3'
    assert body['journal_named'] is True
    assert body['engine_started_at'].startswith('2026-09-22T11:30')
    assert body['snapshot_at']                      # stamped by the engine, not by the caller
    assert body['state']['events'][0]['stage'] == 'LLM'


def test_nothing_collecting_answers_503_with_the_reason_not_an_empty_panel() -> None:
    """Zeros on a screen read as "quiet", and "no workers" is not quiet — it is not measuring."""
    response = _get(_client(with_provider=False), '/v1/dashboard/engine')

    assert response.status_code == 503
    assert 'without workers' in response.json()['detail']


def test_an_unknown_view_is_404_for_a_caller_entitled_to_ask() -> None:
    """The name set is closed, which is also what makes `{name}` an identity segment."""
    response = _get(_client(with_provider=True), '/v1/dashboard/nope', token=_WIDE)

    assert response.status_code == 404
    assert 'nope' in response.json()['detail']


def test_a_narrow_token_is_refused_before_the_name_is_resolved() -> None:
    """403 and not 404, so the route cannot be used to enumerate what exists.

    The grant is compared against the identity segment, so a token holding `dashboard:engine` asking
    for `dashboard:nope` is denied — and the denial says nothing about whether `nope` is a view. An
    endpoint that answered 404 here would be an existence oracle for anyone with any valid token.

    The proof is the pair: the SAME path answers 403 to a narrow token and 404 to a wide one, so
    the status follows the grant rather than the existence of the view. The body does echo the
    name the caller asked for, which leaks nothing — they supplied it.
    """
    client = _client(with_provider=True)

    assert _get(client, '/v1/dashboard/nope', token=_HOLDER).status_code == 403
    assert _get(client, '/v1/dashboard/nope', token=_WIDE).status_code == 404


def test_a_token_granted_the_signal_streams_cannot_read_the_panel() -> None:
    """Access is granted by name, never inherited: `pipelines:*` says nothing about `dashboard`.

    The panel carries the day's spend, the per-symbol signals and the activity feed — more than the
    consumer contract, and to a different audience.
    """
    response = _get(_client(with_provider=True), '/v1/dashboard/engine', token=_OTHER)

    assert response.status_code == 403


@pytest.mark.parametrize('header', [{}, {'Authorization': 'Bearer wrong-token'}])
def test_it_is_not_reachable_without_a_valid_credential(header: dict) -> None:
    """The surface is behind the same bearer every protected route is."""
    assert _client(with_provider=True).get(
        '/v1/dashboard/engine', headers=header).status_code in (401, 403)
