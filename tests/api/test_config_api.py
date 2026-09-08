"""`GET /v1/configs` (2026-09-08) — the transport half: narrowing, and what each absence means.

The projection and the redaction are the config views' job (`tests/configuration/test_config_view.py`);
**grant filtering needs the bearer layer** and therefore lives in `test_report_scopes.py`, next to
the reports surface it mirrors. What is tested here is what only this route can get wrong: an
unknown name, an unknown id, and the payload the transport assembles from a rendered document.
"""
import json
from pathlib import Path
from typing import Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.endpoints.config_router import build_config_router
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.configuration.app_config_view import AppConfigView
from finiexragengine.configuration.source_set_config_view import SourceSetConfigView
from finiexragengine.configuration.source_set_registry import SourceSetRegistry

def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


@pytest.fixture
def views(tmp_path: Path) -> Dict[str, object]:
    manager = AppConfigManager(
        config_path=_write(tmp_path / 'app_config.json', {'log_level': 'INFO'}),
        user_config_path=_write(tmp_path / 'user' / 'app_config.json',
                                {'telegram': {'bot_token': '8012345678:AAF-secret-value'}}))
    for name in ('crypto_news', 'forex_news'):
        _write(tmp_path / 'sets' / f'{name}.json',
               {'source_set_id': name,
                'sources': [{'source_id': 'a', 'url': f'https://{name}.test/feed'}]})
    sets = SourceSetRegistry(tmp_path / 'sets', tmp_path / 'user_sets')
    sets.load()
    return {'app': AppConfigView(manager), 'source_sets': SourceSetConfigView(sets)}


def _client(views, tokens: TokenRegistry = None) -> TestClient:
    app = FastAPI()
    app.include_router(build_config_router(views, tokens or TokenRegistry()))
    return TestClient(app)


# --- the catalog says only what this caller can fetch ---------------------------------------------

def test_the_catalog_lists_the_documents_and_the_ids_each_one_accepts(views):
    body = _client(views).get('/v1/configs').json()

    listed = {entry['name']: entry for entry in body['configs']}
    assert set(listed) == {'app', 'source_sets'}
    assert listed['source_sets']['ids'] == ['crypto_news', 'forex_news']
    assert listed['app']['ids'] == ['app']
    # The layers say where a document came from, so an overlay is visible before it is read.
    assert any('app_config.json' in layer for layer in listed['app']['layers'])


# --- names, ids, and what each absence means -------------------------------------------------------

def test_an_unknown_document_name_is_a_404_and_cannot_reach_a_file(views):
    client = _client(views)

    assert client.get('/v1/configs/nope').status_code == 404
    # `{name}` is one path segment, so a traversal attempt does not even match the route — the
    # views are a dict lookup, never a path resolved against the filesystem.
    assert client.get('/v1/configs/app/../../etc/passwd').status_code == 404


def test_an_unknown_id_is_a_404_that_names_it(views):
    response = _client(views).get('/v1/configs/source_sets?id=nope')

    assert response.status_code == 404
    assert 'nope' in response.json()['detail']


def test_narrowing_returns_one_document_without_changing_the_shape(views):
    body = _client(views).get('/v1/configs/source_sets?id=crypto_news').json()

    assert list(body['documents']) == ['crypto_news']
    assert body['name'] == 'source_sets'
    assert body['documents']['crypto_news']['sources'][0]['source_id'] == 'a'


# --- what the route must never serve ----------------------------------------------------------------

def test_the_served_document_carries_no_credential_and_says_where_it_masked(views):
    body = _client(views).get('/v1/configs/app').json()

    assert 'AAF-secret-value' not in json.dumps(body)
    assert body['documents']['app']['telegram']['bot_token'] == '«redacted»'
    assert 'telegram.bot_token' in body['redacted']
    assert body['unclassified'] == []


def test_a_generated_at_is_stamped_so_a_cached_answer_is_recognisable(views):
    body = _client(views).get('/v1/configs/app').json()

    assert body['generated_at'].endswith('Z') or '+00:00' in body['generated_at']
