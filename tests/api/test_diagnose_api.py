"""`GET /v1/diagnose/feed` (2026-09-09) — the first route that reaches outward on request.

Every other route reads the store or a local file. This one fetches, which is the diagnosis: on
2026-09-09 a feed that was well-formed from the dev container was unparseable on the server, and
nothing reachable remotely could say what those bytes actually were.

That difference is the whole subject here. The cases below are the properties that keep it a
diagnostic rather than an amplifier or a proxy, plus the redaction that lets it carry remote bytes
at all.
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.api.endpoints import diagnose_router as router_module
from finiexragengine.api.endpoints.diagnose_router import build_diagnose_router
from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.sources.feed_doctor import FeedDiagnosis


def _registry(tmp_path: Path) -> SourceSetRegistry:
    path = tmp_path / 'sets' / 'forex_news.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        'source_set_id': 'forex_news',
        'sources': [
            {'source_id': 'boj_press', 'url': 'https://www.boj.or.jp/en/rss/whatsnew.xml'},
            # Disabled on purpose: the doctor is how an operator asks whether a switched-off feed
            # has become reachable again, so it must remain probeable.
            {'source_id': 'fxstreet', 'url': 'https://fx.test/rss', 'enabled': False},
        ]}), encoding='utf-8')
    registry = SourceSetRegistry(tmp_path / 'sets', tmp_path / 'user_sets')
    registry.load()
    return registry


def _client(registry) -> TestClient:
    app = FastAPI()
    app.include_router(build_diagnose_router(registry, TokenRegistry()))
    return TestClient(app)


def _diagnosis(**overrides) -> FeedDiagnosis:
    fields = dict(source_id='boj_press', url='https://www.boj.or.jp/en/rss/whatsnew.xml',
                  http_status=200, content_type='application/xml;charset=UTF-8',
                  body_bytes=14722, entries=44, head='<?xml version="1.0" encoding="UTF-8" ?>',
                  verdict='OK')
    fields.update(overrides)
    return FeedDiagnosis(**fields)


# --- what keeps it from becoming an amplifier -----------------------------------------------------

def test_source_id_is_required_and_never_defaults_to_every_feed(tmp_path, monkeypatch):
    """The security property, asserted rather than assumed.

    The CLI's default is *all* feeds — 39 of them at two requests each. As a GET that shape is a
    78-request amplifier that also perturbs the feeds whose health it reports on. A default here is
    exactly how that would arrive later, by accident.
    """
    monkeypatch.setattr(router_module, 'diagnose_feed',
                        lambda *args, **kwargs: pytest.fail('probed with no source_id'))

    response = _client(_registry(tmp_path)).get('/v1/diagnose/feed')

    assert response.status_code == 422
    assert 'source_id' in response.text


def test_an_unknown_feed_is_refused_before_anything_leaves_the_process(tmp_path, monkeypatch):
    """A caller names a feed this engine polls — never a host, and never one we do not have."""
    monkeypatch.setattr(router_module, 'diagnose_feed',
                        lambda *args, **kwargs: pytest.fail('probed an unconfigured feed'))
    client = _client(_registry(tmp_path))

    response = client.get('/v1/diagnose/feed?source_id=not_ours')

    assert response.status_code == 404
    assert 'not_ours' in response.json()['detail']


def test_an_unknown_probe_name_is_a_404(tmp_path):
    assert _client(_registry(tmp_path)).get('/v1/diagnose/nope?source_id=boj_press').status_code == 404


def test_without_a_catalogue_it_says_so_rather_than_answering_for_nothing():
    """Scaffold-mock mode loaded no feeds; an empty 404 would read as "we do not have that feed"."""
    response = _client(None).get('/v1/diagnose/feed?source_id=boj_press')

    assert response.status_code == 503
    assert 'scaffold-mock' in response.json()['detail']


def test_a_disabled_feed_is_still_probeable(tmp_path, monkeypatch):
    """Asking whether a switched-off feed has recovered is precisely a question about one."""
    seen = {}

    def _record(source_id, url, **kwargs):
        seen.update(source_id=source_id, disabled=kwargs.get('disabled'))
        return _diagnosis(source_id=source_id, url=url, disabled=True)

    monkeypatch.setattr(router_module, 'diagnose_feed', _record)

    body = _client(_registry(tmp_path)).get('/v1/diagnose/feed?source_id=fxstreet').json()

    assert seen == {'source_id': 'fxstreet', 'disabled': True}
    assert body['diagnosis']['disabled'] is True


def test_the_probe_is_given_a_deadline_shorter_than_the_units_default(tmp_path, monkeypatch):
    """It runs in the pool that serves every other sync endpoint, so the wait has to be bounded."""
    seen = {}
    monkeypatch.setattr(router_module, 'diagnose_feed',
                        lambda source_id, url, **kwargs: seen.update(kwargs) or _diagnosis())

    _client(_registry(tmp_path)).get('/v1/diagnose/feed?source_id=boj_press')

    assert seen['timeout'] <= 10


# --- carrying bytes this engine did not write -----------------------------------------------------

def test_a_credential_in_the_url_or_the_body_is_masked_and_named(tmp_path, monkeypatch):
    """`head` is the point of the route and, by construction, arbitrary remote content."""
    monkeypatch.setattr(router_module, 'diagnose_feed', lambda *args, **kwargs: _diagnosis(
        url='https://feeds.example.com/rss?apikey=SEKRET123',
        head='HTTP/1.1 401\r\nAuthorization: Bearer abc123DEF456ghi\r\n',
        bozo_exception='not well-formed while fetching '
                       'https://feeds.example.com/rss?apikey=SEKRET123'))

    body = _client(_registry(tmp_path)).get('/v1/diagnose/feed?source_id=boj_press').json()

    raw = json.dumps(body)
    assert 'SEKRET123' not in raw and 'abc123DEF456ghi' not in raw
    assert set(body['redacted']) == {'url', 'head', 'bozo_exception'}
    # The surrounding text survives — a masked line still has to answer the question it was read for.
    assert body['diagnosis']['url'].startswith('https://feeds.example.com/rss?apikey=')


def test_a_clean_diagnosis_is_not_altered_and_says_nothing_was(tmp_path, monkeypatch):
    monkeypatch.setattr(router_module, 'diagnose_feed', lambda *args, **kwargs: _diagnosis())

    body = _client(_registry(tmp_path)).get('/v1/diagnose/feed?source_id=boj_press').json()

    assert body['redacted'] == []
    assert body['diagnosis']['head'] == '<?xml version="1.0" encoding="UTF-8" ?>'


# --- the API must not disagree with the console ----------------------------------------------------

def test_the_payload_carries_the_fields_the_console_renders(tmp_path, monkeypatch):
    """One shape, serialized once: a second mirror model is how two surfaces come to disagree."""
    monkeypatch.setattr(router_module, 'diagnose_feed', lambda *args, **kwargs: _diagnosis())

    body = _client(_registry(tmp_path)).get('/v1/diagnose/feed?source_id=boj_press').json()

    diagnosis = body['diagnosis']
    assert diagnosis['http_status'] == 200 and diagnosis['body_bytes'] == 14722
    assert diagnosis['entries'] == 44 and diagnosis['verdict'] == 'OK'
    for key in ('source_id', 'url', 'bozo', 'suspicious', 'max_age_hours', 'age_basis'):
        assert key in diagnosis, key
    assert body['source_id'] == 'boj_press' and body['name'] == 'feed'
