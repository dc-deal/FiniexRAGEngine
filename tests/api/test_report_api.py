"""`GET /v1/reports` (ISSUE_104) — the transport half: parameters, bounds, and who may call.

What the reports *contain* is `observability/reports/test_report_catalog.py`'s business. Here: that an unknown name reads
as "no such report" rather than "the report is broken", that a window can never be unbounded, and
that a report added later is protected without anyone remembering to protect it.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.endpoints.report_router import build_report_router
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports import report_catalog
from finiexragengine.types.report_types import ReportParams

_TOKEN = 'report-suite-token'


@pytest.fixture
def client(clean_db: str) -> TestClient:
    app = FastAPI()
    # No consumer on the request (authentication is not mounted here), so every report is
    # readable — the scope is exercised in `test_report_scopes.py`.
    app.include_router(build_report_router(clean_db, AppConfigManager(), TokenRegistry(),
                                           max_window_days=90))
    return TestClient(app)


def test_the_catalog_lists_every_report_with_its_parameters(client: TestClient) -> None:
    body = client.get('/v1/reports').json()

    names = {entry['name'] for entry in body['reports']}
    assert {'source_health', 'source_latency', 'breaking'} <= names
    assert body['max_window_days'] == 90


def test_a_report_answers_with_its_data_and_the_window_it_used(client: TestClient) -> None:
    body = client.get('/v1/reports/source_health').json()

    assert body['report'] == 'source_health'
    assert body['since'] is None             # this report has no window, and says so
    # ...and the parameter it does take comes back with its origin attached.
    assert body['params']['recent_problems']['source'] == 'config'
    assert 'rows' in body['data']
    # The derived values the console shows must be in the payload too, or the two surfaces
    # disagree about the same report.
    assert 'flagged_count' in body['data']


def test_an_unknown_report_is_404_not_500(client: TestClient) -> None:
    """"There is no such report" and "the report is broken" are different answers to a caller."""
    response = client.get('/v1/reports/does_not_exist')

    assert response.status_code == 404
    assert 'does_not_exist' in response.json()['detail']


def test_a_missing_required_parameter_is_422_and_names_it(client: TestClient) -> None:
    response = client.get('/v1/reports/source_quarantine')

    assert response.status_code == 422
    assert 'source_id' in response.json()['detail']


def test_an_unparseable_window_is_422(client: TestClient) -> None:
    response = client.get('/v1/reports/breaking?window=last-tuesday')

    assert response.status_code == 422


def test_a_window_beyond_the_ceiling_is_clamped_and_says_so(clean_db: str) -> None:
    """An unbounded window is a full journal scan, and `all` is exactly what a diagnostic tool asks
    for. It is clamped rather than refused — and the response states which window it really used,
    so a caller never has to infer whether it got what it asked for."""
    app = FastAPI()
    app.include_router(build_report_router(clean_db, AppConfigManager(), TokenRegistry(),
                                           max_window_days=30))
    body = TestClient(app).get('/v1/reports/breaking?window=all').json()

    assert body['params']['window']['clamped'] is True
    assert body['params']['window']['value'] == '30d'
    assert body['params']['window']['source'] == 'request'   # asked for, then shortened
    since = datetime.fromisoformat(body['since'].replace('Z', '+00:00'))
    assert since > datetime.now(timezone.utc) - timedelta(days=31)


def test_a_window_inside_the_ceiling_is_untouched(client: TestClient) -> None:
    body = client.get('/v1/reports/breaking?window=7d').json()

    assert body['params']['window']['clamped'] is False
    assert body['params']['window']['value'] == '7d'
    assert body['params']['window']['source'] == 'request'


def test_a_report_added_to_the_catalog_is_reachable_without_touching_a_route(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ISSUE_98 property, applied to this surface: the route is generic, so a new report is a
    catalog entry — it cannot be added in a way that skips the transport's rules."""
    spec = report_catalog.ReportSpec(
        build=lambda dsn, manager, params: {'answer': 42},
        summary='a report registered inside this test')
    monkeypatch.setitem(report_catalog._CATALOG, 'invented_report', spec)

    listing = client.get('/v1/reports').json()
    assert 'invented_report' in {entry['name'] for entry in listing['reports']}

    body = client.get('/v1/reports/invented_report').json()
    assert body['data'] == {'answer': 42}


def test_a_parameter_the_report_cannot_use_is_refused_not_dropped(client: TestClient) -> None:
    """Accepting a parameter and then ignoring it is the failure this surface is built against."""
    response = client.get('/v1/reports/breaking?source_id=theblock')

    assert response.status_code == 422
    detail = response.json()['detail']
    assert 'source_id' in detail and 'window' in detail      # what it cannot take, and what it can


def test_a_cap_can_be_raised_for_one_call(client: TestClient) -> None:
    body = client.get('/v1/reports/source_health?recent_problems=25').json()

    assert body['params']['recent_problems']['value'] == 25
    assert body['params']['recent_problems']['source'] == 'request'


def test_a_cap_is_bounded_like_the_window(client: TestClient) -> None:
    """A query parameter that sets a row count is an unbounded query unless it is bounded."""
    assert client.get('/v1/reports/source_health?recent_problems=100000').status_code == 422


def test_the_sweep_sample_is_bounded_because_weight_is_what_made_it_doubtful(
        client: TestClient) -> None:
    """`detection_sweep` is the heaviest entry on the catalog — a self-join over embeddings.

    It belongs here because it cannot spend (ISSUE_106), not because it is cheap. The bound is what
    makes admitting it safe, so it is asserted rather than trusted: an unbounded `sample` would be
    the one way this entry differs in kind from every other read.
    """
    assert client.get('/v1/reports/detection_sweep?sample=99999').status_code == 422
    assert client.get('/v1/reports/detection_sweep?sample=0').status_code == 422

    body = client.get('/v1/reports/detection_sweep?sample=25').json()
    assert body['params']['sample']['value'] == 25
    assert body['params']['sample']['source'] == 'request'


def test_the_sweep_grid_can_be_overridden_and_its_values_are_bounded(
        client: TestClient) -> None:
    """The grid is a list parameter, following `cost.windows` — so it is asserted, not assumed.

    A repeated query parameter with per-item bounds is the one shape on this route that behaves
    differently from a scalar, and a similarity outside [0, 1] is not a grid, it is a typo.
    """
    body = client.get('/v1/reports/detection_sweep?similarities=0.9&similarities=0.5').json()
    assert body['params']['similarities']['value'] == [0.9, 0.5]
    assert body['params']['similarities']['source'] == 'request'

    assert client.get('/v1/reports/detection_sweep?similarities=1.5').status_code == 422

    # Nothing supplied: the configured grid stands, and the answer says it came from config.
    configured = client.get('/v1/reports/detection_sweep').json()
    assert configured['params']['similarities']['source'] == 'config'


def test_the_sweep_narrows_to_one_set_over_http(client: TestClient) -> None:
    every = client.get('/v1/reports/detection_sweep').json()['data']
    narrowed = client.get(
        '/v1/reports/detection_sweep?source_set_id=crypto_news').json()['data']

    assert len(every) >= len(narrowed)
    assert [report['source_set_id'] for report in narrowed] == ['crypto_news']


def test_retrieval_drift_takes_a_window_and_nothing_a_caller_could_bend(
        client: TestClient) -> None:
    """`min_passes` decides whether a cell reads as thin — a verdict, so it stays config-only.

    Same rule as `source_health.silence_days`: a caller must not be able to make the same cell look
    solid or thin.
    """
    assert client.get('/v1/reports/retrieval_drift?window=14d').status_code == 200
    assert client.get('/v1/reports/retrieval_drift?symbol=BTCUSD').status_code == 422


def test_cost_compares_the_configured_set_and_a_call_narrows_it(client: TestClient) -> None:
    configured = client.get('/v1/reports/cost').json()
    assert [window['label'] for window in configured['data']['real']] == [
        'this week', 'this month', 'all-time']
    assert configured['params']['windows']['source'] == 'config'

    narrowed = client.get('/v1/reports/cost?window=14d').json()
    assert [window['label'] for window in narrowed['data']['real']] == ['last 14d']
    assert 'windows' not in narrowed['params']              # superseded, so not reported as applied


def test_remaining_credit_stays_an_all_time_fact_whichever_window_is_shown(
        client: TestClient) -> None:
    """`spent_all_usd` feeds `remaining_usd`; it must not follow the displayed window."""
    full = client.get('/v1/reports/cost').json()['data']
    narrowed = client.get('/v1/reports/cost?window=14d').json()['data']

    assert full['spent_all_usd'] == narrowed['spent_all_usd']


def test_the_corpus_text_report_is_served_with_its_payload(client: TestClient) -> None:
    """ISSUE_112's durable half has to be reachable remotely, or it is a shell-only answer again.

    The engine runs on a box the assistant can only reach over the read-only HTTPS surface, so a
    diagnostic that exists solely as a CLI answers nobody who is not already on the machine. This
    pins the whole chain: the catalog lists it, the generic route builds it, and the payload
    carries the fields the console renders — including `treatments`, whose per-slice carrier counts
    are the one number that says whether the normaliser is working.
    """
    listing = client.get('/v1/reports').json()
    assert 'corpus_text' in {entry['name'] for entry in listing['reports']}

    body = client.get('/v1/reports/corpus_text').json()

    assert body['report'] == 'corpus_text'
    assert body['params']['window']['source'] == 'config'
    # The census and the phantom table both travel — a payload carrying only the totals would make
    # the API a strictly weaker surface than the console for the same report.
    for key in ('articles', 'treatments', 'removal', 'phantoms', 'window_articles', 'keyword_sets'):
        assert key in body['data'], key
