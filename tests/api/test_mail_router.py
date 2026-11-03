"""`GET /v1/mail/status` (#138): behind `mail:status`, no account on the wire."""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.api.api_app import _build_protected_router
from finiexragengine.api.endpoints.mail_router import build_mail_router
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.config_types.app_config_types import ApiConfig
from finiexragengine.types.mail_types import MailSourceStatus, MailWindowCounts


def _client() -> TestClient:
    api_config = ApiConfig(tokens={
        'mail': {'token': 'mail-token', 'grants': ['mail:status']},
        'other': {'token': 'other-token', 'grants': ['reports:source_health']}})
    tokens = TokenRegistry(api_config.tokens)
    running = MailSourceStatus(source_id='newsletter_a', state='ok', account='bot@example.com',
                               last_poll_at=datetime(2026, 11, 3, 12, tzinfo=timezone.utc),
                               today=MailWindowCounts(mails=3, items=34), extract_backlog=1)
    manager = AppConfigManager()
    registry = PipelineRegistry(manager.get_pipelines_dir(), manager.get_user_pipelines_dir())
    registry.load()
    app = FastAPI()
    # Mounted the way the app mounts it: behind the bearer check that names the caller.
    app.include_router(_build_protected_router(registry, api_config, tokens, extra_routers=[
        build_mail_router(tokens, ['newsletter_a', 'newsletter_b'], None, lambda: [running])]))
    return TestClient(app)


def _get(token: str, name: str = 'status'):
    return _client().get(f'/v1/mail/{name}', headers={'Authorization': f'Bearer {token}'})


def test_the_status_answers_running_and_configured_sources_without_the_account():
    response = _get('mail-token')
    assert response.status_code == 200
    sources = {source['source_id']: source for source in response.json()['sources']}
    assert sources['newsletter_a']['state'] == 'ok' and sources['newsletter_a']['today']['items'] == 34
    assert sources['newsletter_b']['state'] == 'not_polled_here'
    assert 'bot@example.com' not in response.text and 'account' not in response.text


def test_another_grant_is_refused_and_an_unknown_name_is_404():
    assert _get('other-token').status_code == 403
    assert _get('mail-token', name='everything').status_code == 403   # the grant names `status` only
    assert _get('not-a-token').status_code == 401
