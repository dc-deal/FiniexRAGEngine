"""Per-token report scopes (ISSUE_104) — needs a reachable Postgres (skipped otherwise).

A verified consumer is not automatically entitled to every report. Spend belongs to the operator;
feed diagnostics may belong to a collector. So each token declares what it reads, and the surface
honours it in both directions — the listing shows only what the caller can fetch, and the fetch
refuses what the listing did not show.

The rule these tests exist to keep: **access is granted by writing a name down, never by omission.**
"""
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.api_app import _build_protected_router
from finiexragengine.api.endpoints.report_router import build_report_router
from finiexragengine.api.endpoints.stream_router import build_stream_router
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.api.grant_auth import build_grant_dependency
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.types.config_types.app_config_types import StreamConfig
from finiexragengine.types.config_types.app_config_types import ApiConfig

_NARROW = 'narrow-consumer-token'
_WIDE = 'wide-consumer-token'


def _pipelines() -> PipelineRegistry:
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    return registry


def _app(clean_db: str) -> FastAPI:
    """The whole surface as `create_app` assembles it: one registry, guard plus reports.

    The registry is constructed directly rather than through `TokenRegistry.load`, because the
    suite sets `FINIEX_API_TOKENS` for every test and **the environment wins** — resolution
    precedence is `test_api_auth.py`'s subject, and letting it apply here would quietly replace the
    scoped tokens these tests are about with an unscoped one.
    """
    api_config = ApiConfig(tokens={
        'ide': {'token': _NARROW, 'grants': ['reports:source_health', 'reports:breaking'],
                'note': 'Testing IDE'},
        'claude-dev': {'token': _WIDE, 'grants': ['*'], 'note': 'assistant'}})
    tokens = TokenRegistry(api_config.tokens)
    app = FastAPI()
    app.include_router(_build_protected_router(
        _pipelines(), api_config, tokens,
        extra_routers=[build_report_router(clean_db, AppConfigManager(), tokens)]))
    return app


@pytest.fixture
def client(clean_db: str) -> TestClient:
    return TestClient(_app(clean_db))


def _as(token: str) -> dict:
    return {'Authorization': f'Bearer {token}'}


def test_the_listing_shows_only_what_this_caller_can_fetch(client: TestClient) -> None:
    """Otherwise every scope becomes a discovery of a 403."""
    narrow = {e['name'] for e in client.get('/v1/reports', headers=_as(_NARROW)).json()['reports']}
    wide = {e['name'] for e in client.get('/v1/reports', headers=_as(_WIDE)).json()['reports']}

    assert narrow == {'source_health', 'breaking'}
    assert 'cost' in wide and narrow < wide


def test_a_report_outside_the_scope_is_refused_and_the_refusal_is_debuggable(
        client: TestClient) -> None:
    """403, not 404: the report exists, and a partner who can read the documentation gains nothing
    from us pretending otherwise — while a denial they can debug saves a round of questions."""
    response = client.get('/v1/reports/cost', headers=_as(_NARROW))

    assert response.status_code == 403
    detail = response.json()['detail']
    assert 'ide' in detail                       # who they are
    assert 'source_health' in detail             # what they may read instead


def test_a_report_inside_the_scope_answers(client: TestClient) -> None:
    assert client.get('/v1/reports/source_health', headers=_as(_NARROW)).status_code == 200


def test_the_wildcard_reads_everything(client: TestClient) -> None:
    assert client.get('/v1/reports/cost', headers=_as(_WIDE)).status_code == 200
    assert client.get('/v1/reports/source_health', headers=_as(_WIDE)).status_code == 200


def test_a_scoped_caller_cannot_tell_an_unknown_report_from_a_forbidden_one(
        client: TestClient) -> None:
    """Authorisation runs before resolution, so a scoped caller gets 403 either way.

    That is deliberate: answering 404 for a name they are not entitled to anyway would turn the
    endpoint into an existence oracle — probe a name, read the status, learn whether we have such a
    report. It is the same reasoning as the identical 401 body for every credential failure.

    The catalog listing already tells each caller exactly what exists *for them*, so nothing useful
    is withheld; what is withheld is the ability to enumerate what does not.
    """
    assert client.get('/v1/reports/no_such_report', headers=_as(_NARROW)).status_code == 403


def test_a_caller_entitled_to_everything_still_gets_404_for_a_typo(client: TestClient) -> None:
    """Absence is informative only to someone entitled to the thing that is absent."""
    assert client.get('/v1/reports/no_such_report', headers=_as(_WIDE)).status_code == 404


def test_an_unauthenticated_call_never_reaches_the_scope_check(client: TestClient) -> None:
    assert client.get('/v1/reports/source_health').status_code == 401
    assert client.get('/v1/reports').status_code == 401


# --- the configuration side ------------------------------------------------------------------

def test_a_token_must_declare_what_it_reads() -> None:
    """The mandatory field is the whole point: a default would be a grant nobody wrote down."""
    with pytest.raises(Exception) as excinfo:
        ApiConfig(tokens={'ide': {'token': 'x'}})

    assert 'grants' in str(excinfo.value)


def test_the_old_bare_string_form_is_refused_with_the_shape_that_replaces_it() -> None:
    """Absorbing it as '*' would reinstate by-omission access for exactly the entries nobody has
    looked at in a while."""
    with pytest.raises(Exception) as excinfo:
        ApiConfig(tokens={'ide': 'a-bare-token'})

    message = str(excinfo.value)
    assert 'ide' in message and '"grants": ["*"]' in message


def test_an_environment_supplied_token_reads_everything() -> None:
    """`FINIEX_API_TOKENS="name:token"` has nowhere to put a scope, and that path exists for a
    container or CI — environments this project owns rather than hands to a consumer."""
    registry = TokenRegistry({'ci': 'a-plain-token'})

    assert registry.grants_of('ci') == '*'
    assert registry.may('ci', 'reports:cost') is True


def test_an_unknown_consumer_is_denied_rather_than_defaulted() -> None:
    """Reaching this with a name the registry does not hold is a bug, and a bug must not grant."""
    registry = TokenRegistry({'ide': 'x'})

    assert registry.may('nobody', 'reports:source_health') is False


def test_an_empty_scope_reads_nothing(clean_db: str) -> None:
    """A token can exist and be allowed nothing — useful for one that only calls /latest."""
    api_config = ApiConfig(tokens={'signals': {'token': 'signals-token',
                                              'grants': ['pipelines:*']}})
    tokens = TokenRegistry(api_config.tokens)      # direct, for the reason in `_app`
    app = FastAPI()
    app.include_router(_build_protected_router(
        _pipelines(), api_config, tokens,
        extra_routers=[build_report_router(clean_db, AppConfigManager(), tokens)]))
    client = TestClient(app)

    assert client.get('/v1/reports', headers=_as('signals-token')).json()['reports'] == []
    assert client.get('/v1/reports/source_health',
                      headers=_as('signals-token')).status_code == 403
    # ...but the signal path it exists for is untouched.
    assert client.get('/v1/pipelines', headers=_as('signals-token')).status_code == 200


def test_no_route_with_an_identity_segment_is_ungated(clean_db: str) -> None:
    """The structural guarantee, asserted behaviourally rather than by introspection.

    The surface half of a grant is declared per router (`Security(..., scopes=['reports'])`), which
    is the one thing this design can forget: a router mounted without it would be reachable by any
    authenticated consumer. So instead of trusting the declaration, this walks every registered
    route that carries an identity segment and asserts a token holding **nothing** is refused.

    A new router without a declared surface fails here, in the suite, rather than in production —
    the same shape as ISSUE_98's test that a route added later inherits the bearer dependency.
    """
    api_config = ApiConfig(tokens={'empty': {'token': 'holds-nothing', 'grants': []}})
    tokens = TokenRegistry(api_config.tokens)
    app = FastAPI()
    # Every domain router that carries an identity segment belongs in this app, or the walk below
    # cannot see it. The stream (ISSUE_9) is the newest one, and it is the reason its address is
    # `/v1/stream/{pipeline_id}` rather than `?pipeline=`: a query-parameter route would be
    # authenticated, ungated, and invisible to exactly this test.
    store = OutcomeStore(clean_db)
    stream_config = StreamConfig()
    app.include_router(_build_protected_router(
        _pipelines(), api_config, tokens,
        extra_routers=[
            build_report_router(clean_db, AppConfigManager(), tokens),
            build_stream_router(
                StreamDispatcher(store, clean_db),
                StreamReplay(store, stream_config.replay_window_hours),
                _pipelines(), stream_config, build_grant_dependency(tokens)),
        ]))
    client = TestClient(app)

    identity_routes = [(path, method)
                       for path, operations in app.openapi()['paths'].items()
                       for method in operations
                       if '{' in path]
    assert identity_routes, 'no identity routes found — the walk itself is broken'

    for path, method in identity_routes:
        # Any value will do: the grant is refused before the name is resolved, which is the point.
        url = path.replace('{pipeline_id}', 'crypto_sentiment').replace('{name}', 'source_health')
        response = client.request(method.upper(), url, headers=_as('holds-nothing'))
        assert response.status_code == 403, f'{method.upper()} {path} is not gated'
