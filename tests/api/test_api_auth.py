"""Connection security (ISSUE_98) — the evidence set, not a description of it.

Every case here answers one line of the issue's verification list. The one that matters most is
`test_a_route_added_later_inherits_the_dependency`: the whole design is that authentication sits on
the router rather than on the routes, precisely so that the failure this issue exists to prevent —
an endpoint shipped unprotected because somebody forgot — is not something anyone *can* forget.
A guarantee that lives only in a convention is one that gets violated by someone who believes they
are complying.
"""
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.api_app import (
    _build_protected_router,
    _build_public_router,
    create_app,
)
from finiexragengine.api.endpoints.build_router import build_build_router
from finiexragengine.api.endpoints.health_router import build_health_router
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.core.observability.build_info import sample_build_info
from finiexragengine.api.rate_limiter import RateLimiter, client_key
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import ApiConfig

_TOKEN = 'a-token-that-is-not-a-real-credential'
_PROTECTED = ('/v1/pipelines', '/v1/pipelines/crypto_sentiment/latest')


def _registry() -> PipelineRegistry:
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    return registry


def _tokens(api_config: ApiConfig) -> TokenRegistry:
    """The registry `create_app` builds and hands to both the guard and the report surface."""
    return TokenRegistry.load(api_config.tokens)


def _protected_app(**config: object) -> FastAPI:
    """A FastAPI app carrying only the protected router, built the way `create_app` builds it."""
    api_config = ApiConfig(**config)
    app = FastAPI()
    app.include_router(_build_protected_router(_registry(), api_config, _tokens(api_config)))
    return app


def _exempt_app(**config: object) -> FastAPI:
    """The whole surface, built the way `create_app` builds it — both exemptions in play.

    Written against the *mounting decision* rather than against `create_app`, because that decision
    is what was wrong: an exemption switched off has to move to the protected router, not simply
    lose its rate limit.
    """
    manager = AppConfigManager()
    api_config = ApiConfig(**config)
    health = build_health_router(manager)
    build = build_build_router(sample_build_info('0.0.0-test'))
    exempt = ((health, api_config.health_public), (build, api_config.build_info_public))
    app = FastAPI()
    app.include_router(_build_public_router(
        api_config, [router for router, is_public in exempt if is_public]))
    app.include_router(_build_protected_router(
        _registry(), api_config, _tokens(api_config),
        extra_routers=[router for router, is_public in exempt if not is_public]))
    return app


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv('FINIEX_API_TOKENS', f'ide:{_TOKEN}')
    return _TOKEN


# --- the router-level guarantee -------------------------------------------------------------

def test_every_protected_route_rejects_an_unauthenticated_call(tokens: str) -> None:
    client = TestClient(_protected_app())
    for path in _PROTECTED:
        response = client.get(path)
        assert response.status_code == 401, path
        # A well-formed 401 tells a conforming client which scheme to retry with.
        assert response.headers['www-authenticate'] == 'Bearer'


def test_a_route_added_later_inherits_the_dependency(tokens: str) -> None:
    """The point of mounting auth on the router: a new route cannot be added unprotected.

    Nothing in this test touches authentication — it registers an ordinary endpoint on the
    protected router, exactly as a future feature would, and it comes out guarded.
    """
    router = _build_protected_router(_registry(), ApiConfig(), _tokens(ApiConfig()))

    @router.get('/v1/a-future-endpoint-nobody-thought-about')
    def future_endpoint() -> dict:
        return {'secret': 'should never be reachable without a token'}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get('/v1/a-future-endpoint-nobody-thought-about').status_code == 401
    authorised = client.get('/v1/a-future-endpoint-nobody-thought-about',
                            headers={'Authorization': f'Bearer {_TOKEN}'})
    assert authorised.status_code == 200


def test_a_valid_token_passes_and_a_wrong_one_does_not(tokens: str) -> None:
    client = TestClient(_protected_app())
    assert client.get('/v1/pipelines',
                      headers={'Authorization': f'Bearer {_TOKEN}'}).status_code == 200
    for header in (f'Bearer {_TOKEN}-almost', 'Bearer ', f'Basic {_TOKEN}', _TOKEN):
        assert client.get('/v1/pipelines',
                          headers={'Authorization': header}).status_code == 401, header


def test_the_credential_never_reaches_a_log_line_or_a_response_body(
        tokens: str, caplog: pytest.LogCaptureFixture) -> None:
    """A token prefix in a log file is a prefix an attacker with the log no longer has to guess."""
    client = TestClient(_protected_app())
    with caplog.at_level(logging.DEBUG):
        response = client.get('/v1/pipelines', headers={'Authorization': f'Bearer {_TOKEN}'})
        rejected = client.get('/v1/pipelines', headers={'Authorization': f'Bearer {_TOKEN}x'})

    assert _TOKEN not in caplog.text
    assert _TOKEN not in response.text
    assert _TOKEN not in rejected.text
    # Not even a fragment: an eight-character prefix is eight characters nobody has to brute-force.
    assert _TOKEN[:8] not in caplog.text


# --- the switches ---------------------------------------------------------------------------

def test_health_answers_without_a_token(client: TestClient) -> None:
    """The one documented exemption — an uptime probe needs no credential."""
    bare = TestClient(create_app(attach_runners=False))          # no Authorization header at all
    assert bare.get('/v1/health').status_code == 200
    assert client.get('/v1/health').status_code == 200           # and with one, unchanged


def test_the_run_route_does_not_exist_when_disabled(tokens: str) -> None:
    """Not registered, not merely refused.

    A route that answers 403 is still in the OpenAPI schema, still discoverable, and one config
    edit from live. This asserts the stronger property: nothing is mounted at that path.
    """
    disabled = _protected_app()
    assert '/v1/pipelines/{pipeline_id}/run' not in disabled.openapi()['paths']
    assert '/v1/pipelines/{pipeline_id}/latest' in disabled.openapi()['paths']   # sibling intact

    enabled = _protected_app(run_endpoint_enabled=True)
    assert '/v1/pipelines/{pipeline_id}/run' in enabled.openapi()['paths']

    # And behaviourally, which is the difference that matters: with the route absent, routing
    # answers 404 before authentication is ever consulted. With it present, the same unauthorised
    # call is a 401 — i.e. the endpoint exists and is guarded.
    assert TestClient(disabled).post('/v1/pipelines/crypto_sentiment/run').status_code == 404
    assert TestClient(enabled).post('/v1/pipelines/crypto_sentiment/run').status_code == 401


def test_boot_refuses_to_serve_an_unauthenticated_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing tokens is a hard failure, never a warning that starts the engine wide open."""
    monkeypatch.delenv('FINIEX_API_TOKENS', raising=False)
    with pytest.raises(ConfigurationError, match='FINIEX_API_TOKENS'):
        _build_protected_router(_registry(), ApiConfig(require_auth=True),
                                _tokens(ApiConfig(require_auth=True)))
    # ...and with auth deliberately off it builds, because the contract tests need that path.
    _build_protected_router(_registry(), ApiConfig(require_auth=False),
                            _tokens(ApiConfig(require_auth=False)))


# --- the token registry ---------------------------------------------------------------------

def test_the_registry_keeps_hashes_and_not_tokens() -> None:
    registry = TokenRegistry({'ide': _TOKEN})
    assert registry.verify(_TOKEN) == 'ide'
    assert registry.verify(_TOKEN + 'x') is None
    assert registry.names() == ['ide']
    # The plaintext is nowhere in the object — a dump of it is not a credential.
    assert _TOKEN not in repr(vars(registry))


def test_a_malformed_token_variable_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty registry and a broken one must not look the same.

    Silently admitting nobody reads as "not configured", which is exactly the state the boot check
    above turns into a refusal — so a typo would have produced a confusing hard failure instead of
    a precise one.
    """
    for broken in ('no-separator-here', ':missing-name', 'name:', 'ide:a,ide:b'):
        monkeypatch.setenv('FINIEX_API_TOKENS', broken)
        with pytest.raises(ConfigurationError):
            TokenRegistry.load()

    monkeypatch.setenv('FINIEX_API_TOKENS', ' ide : one , collector : two ')
    parsed = TokenRegistry.load()
    assert parsed.names() == ['collector', 'ide']       # whitespace tolerated, order normalised

    monkeypatch.delenv('FINIEX_API_TOKENS')
    assert TokenRegistry.load().is_empty()


# --- the rate limiter -----------------------------------------------------------------------

def test_the_limiter_admits_the_configured_rate_and_then_refuses() -> None:
    limiter = RateLimiter(per_minute=3)
    assert [limiter.allow('client-a') for _ in range(4)] == [True, True, True, False]
    # A different client has its own bucket — the limit is per caller, not global.
    assert limiter.allow('client-b') is True
    # Zero disables it: a deployment can turn the limit off without removing the wiring.
    assert all(RateLimiter(per_minute=0).allow('anyone') for _ in range(100))


def test_the_bucket_is_keyed_on_the_originating_client_not_the_proxy() -> None:
    """Behind the reverse proxy every request arrives from 127.0.0.1.

    Keying on the peer would put every caller in the world into one bucket — a global limit
    wearing the costume of a per-client one, which fails exactly when several consumers are active.
    """
    assert client_key('203.0.113.7, 70.41.3.18', '127.0.0.1') == '203.0.113.7'
    assert client_key(None, '127.0.0.1') == '127.0.0.1'
    assert client_key('', None) == 'unknown'


def test_repeated_failures_are_throttled_before_they_become_a_guessing_machine(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('FINIEX_API_TOKENS', f'ide:{_TOKEN}')
    client = TestClient(_protected_app(auth_failures_per_minute=2))
    codes = [client.get('/v1/pipelines',
                        headers={'Authorization': 'Bearer wrong'}).status_code for _ in range(4)]
    assert codes == [401, 401, 429, 429]
    # A correct credential is never throttled by its neighbours' failures.
    assert client.get('/v1/pipelines',
                      headers={'Authorization': f'Bearer {_TOKEN}'}).status_code == 200


# --- where the tokens come from ---------------------------------------------------------------

def test_the_environment_wins_and_the_config_fills_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Precedence, and the reason it is safe: the source is reported rather than inferred.

    A container and CI set the variable and need no file — `user_configs/` is gitignored, so a
    fresh clone has none. A deployment that prefers one place for every secret sets no variable and
    puts them in the overlay. Both work; which one answered is never a guess.
    """
    monkeypatch.setenv('FINIEX_API_TOKENS', 'from-env:env-token')
    registry = TokenRegistry.load({'from-config': 'config-token'})
    assert registry.names() == ['from-env']
    assert registry.verify('config-token') is None      # the shadowed source is not merged in
    assert registry.source() == 'environment'

    monkeypatch.delenv('FINIEX_API_TOKENS')
    registry = TokenRegistry.load({'from-config': 'config-token'})
    assert registry.verify('config-token') == 'from-config'
    assert registry.source() == 'user_configs'

    assert TokenRegistry.load({}).source() == 'none'


def test_config_tokens_reach_the_protected_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overlay is a first-class source, not a fallback that only half works."""
    monkeypatch.delenv('FINIEX_API_TOKENS', raising=False)
    app = FastAPI()
    app.include_router(_build_protected_router(
        _registry(), ApiConfig(tokens={'ide': {'token': _TOKEN, 'grants': ['*']}}),
        _tokens(ApiConfig(tokens={'ide': {'token': _TOKEN, 'grants': ['*']}}))))
    client = TestClient(app)
    assert client.get('/v1/pipelines').status_code == 401
    assert client.get('/v1/pipelines',
                      headers={'Authorization': f'Bearer {_TOKEN}'}).status_code == 200


def test_the_tracked_config_never_ships_a_token() -> None:
    """A credential in a committed file is a credential in everyone's clone.

    The defaults-mirror test would happily accept one — it only checks that the file and the
    Pydantic defaults agree. This checks the thing that actually matters about `api.tokens`.
    """
    import json
    from pathlib import Path as _Path

    tracked = json.loads((_Path(__file__).resolve().parents[2] / 'configs' / 'app_config.json')
                         .read_text(encoding='utf-8'))
    assert tracked['api']['tokens'] == {}
    assert ApiConfig().tokens == {}


def test_the_schema_endpoints_are_off_unless_asked_for(tokens: str) -> None:
    """FastAPI mounts `/docs`, `/redoc` and `/openapi.json` on the **app**, not on a router.

    So no router-level dependency can protect them — which is exactly the kind of coverage that
    gets assumed. Found while preparing to open the reverse proxy: with a catch-all proxy they
    would have published the full API surface map, unauthenticated, the moment the edge opened.
    Off by default, opt-in like `POST /run`.
    """
    off = TestClient(create_app(attach_runners=False))
    for path in ('/docs', '/redoc', '/openapi.json'):
        assert off.get(path).status_code == 404, path

    # `/health` is unaffected — the exemption is a route, not a hole in the app.
    assert off.get('/v1/health').status_code == 200


# --- the exemptions, in both directions (ISSUE_98 follow-up) --------------------------------

def test_switching_the_health_exemption_off_moves_it_behind_the_token(tokens: str) -> None:
    """The regression this test exists for: the flag used to do the opposite of its name.

    `health_public: false` left /health mounted on the app anyway — still reachable without a
    credential and now *unthrottled*, because the rate limiter lives on the public wrapper. So the
    switch documented as "moves it behind the token" removed the only protection an anonymous
    caller met. Asserted behaviourally, in both positions, because the failure was invisible in a
    reading of either the flag or its docstring.
    """
    assert TestClient(_exempt_app(health_public=True)).get('/v1/health').status_code == 200

    closed = TestClient(_exempt_app(health_public=False))
    assert closed.get('/v1/health').status_code == 401
    assert closed.get('/v1/health',
                      headers={'Authorization': f'Bearer {_TOKEN}'}).status_code == 200


def test_build_is_public_by_default_and_can_be_closed(tokens: str) -> None:
    """`/v1/build` is the second exemption — and it is a switch, not a fixture of the code."""
    open_client = TestClient(_exempt_app())
    response = open_client.get('/v1/build')
    assert response.status_code == 200
    assert response.json()['version'] == '0.0.0-test'

    closed = TestClient(_exempt_app(build_info_public=False))
    assert closed.get('/v1/build').status_code == 401
    assert closed.get('/v1/build',
                      headers={'Authorization': f'Bearer {_TOKEN}'}).status_code == 200


def test_the_build_payload_is_sampled_once_not_per_request(tokens: str) -> None:
    """Two calls must return the identical process start time.

    The point of the field is to describe the code this process imported; a value re-read per
    request would describe the working tree instead, and would differ between two calls the moment
    someone pulled without restarting — the exact case it exists to catch.
    """
    client = TestClient(_exempt_app())
    first = client.get('/v1/build').json()
    second = client.get('/v1/build').json()

    assert first == second
    assert first['started_at'] == second['started_at']


def test_the_report_surface_requires_a_token_on_the_real_app(tokens: str) -> None:
    """ISSUE_104's routes are mounted on the protected router, so they inherit the guard.

    Asserted against `create_app` rather than a hand-built router: the mounting decision is what
    could go wrong here, and it lives there. `/v1/reports` publishes source health, quarantine
    history and the breaking funnel — operational detail that has no business being anonymous.
    """
    app = create_app(attach_runners=False)
    if '/v1/reports' not in app.openapi()['paths']:
        pytest.skip('no DATABASE_URL — the report surface is not mounted without a store')
    client = TestClient(app)

    assert client.get('/v1/reports').status_code == 401
    assert client.get('/v1/reports/source_health').status_code == 401
    assert client.get('/v1/reports',
                      headers={'Authorization': f'Bearer {_TOKEN}'}).status_code == 200
