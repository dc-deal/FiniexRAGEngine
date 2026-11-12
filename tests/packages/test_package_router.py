"""Signal packages over HTTP — the collector's contract, the receipt rules, the open routes.

No database: the packages are written by hand into a temporary folder (`_make_package`), so these
tests pin the routes and their rules, and `test_day_packer` pins how a package is made. Pipeline
names are neutral; `stream_p` stands for a private pipeline.
"""
import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Tuple

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from finiex_auth.bearer_auth import build_bearer_dependency
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.api.endpoints.package_router import build_package_routers, public_package_names
from finiexragengine.core.packages.day_packer import build_manifest
from finiexragengine.core.packages.package_ledger import PackageLedger
from finiexragengine.core.packages.package_schedule import PackageSchedule
from finiexragengine.core.packages.package_store import MANIFEST_SUFFIX, PackageStore, write_json_atomically
from finiexragengine.types.config_types.app_config_types import ApiConfig, PackagesConfig

_NOW = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
_TOKENS = {
    'ide': {'token': 'ide-token-0123456789', 'grants': ['packages:*', 'receipts:*']},
    'other': {'token': 'other-token-0123456789', 'grants': ['packages:*', 'receipts:*']},
    'rehearsal': {'token': 'rehearsal-token-012345', 'grants': ['packages:*', 'receipts:*']},
    'operator': {'token': 'operator-token-0123456', 'grants': ['*']},
    'reports_only': {'token': 'reports-token-0123456', 'grants': ['reports:*']},
}


def _auth(name: str) -> Dict[str, str]:
    return {'Authorization': f"Bearer {_TOKENS[name]['token']}"}


def _make_package(store: PackageStore, source: str, sequence: int, cutoff: datetime,
                  payload: bytes = b'{"a": 1}\n' * 2000) -> Tuple[str, bytes]:
    """One committed package as the packer would leave it: zip, then manifest."""
    package_id = f'{source}-{sequence:04d}-{cutoff:%Y%m%d}'
    name = f'{source}_{(cutoff - timedelta(days=1)):%Y-%m-%d}.jsonl'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    data = buffer.getvalue()
    ready = store.ready_dir(source)
    ready.mkdir(parents=True, exist_ok=True)
    (ready / f'{package_id}.zip').write_bytes(data)
    manifest = build_manifest(
        source=source, package_id=package_id, sequence=sequence, previous_package_id=None,
        cutoff=cutoff, created_at=cutoff + timedelta(hours=1, minutes=30), mint='mint00000001',
        zip_info={'name': f'{package_id}.zip', 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()},
        members=[{'name': name, 'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
                  'record_count': 2000, 'first_collected_msc': 1, 'last_collected_msc': 2,
                  'config_fingerprints': []}],
        excluded=[], span_from=cutoff - timedelta(days=7), outages=None, starts=None,
        packer={'program': 'finiex-rag-packages', 'version': 'test', 'commit': None, 'dirty': None})
    write_json_atomically(ready / f'{package_id}{MANIFEST_SUFFIX}', manifest)
    return package_id, data


class _Service:
    def __init__(self, root: Path) -> None:
        self.config = PackagesConfig(enabled=True, output_dir=str(root), dev_consumers=['rehearsal'])
        self.store, self.ledger = PackageStore(root), PackageLedger(root)
        self.schedule = PackageSchedule(root, self.config, lambda: ['stream_a', 'stream_p'])
        tokens = TokenRegistry(ApiConfig(tokens=_TOKENS).tokens)
        open_router, gated = build_package_routers(
            store=self.store, ledger=self.ledger, schedule=self.schedule, config=self.config,
            tokens=tokens, loop_status=lambda: {'started_at': _NOW.isoformat(), 'next_run': None,
                                                'next_run_set_aside': False, 'next_special': None,
                                                'packing': False},
            heartbeat_status=lambda: {'enabled': False, 'sent': 0, 'withheld': 0, 'failed': 0},
            packer={'program': 'finiex-rag-packages'}, public_names=public_package_names(['stream_p']),
            clock=lambda: _NOW)
        self.app = FastAPI()
        self.app.include_router(open_router)                     # the open routes come first
        protected = APIRouter(dependencies=[Depends(build_bearer_dependency(tokens))])
        protected.include_router(gated)
        self.app.include_router(protected)
        self.client = TestClient(self.app)


@pytest.fixture
def service(tmp_path: Path) -> _Service:
    return _Service(tmp_path / 'packages')


@pytest.fixture
def package(service: _Service) -> Tuple[str, bytes]:
    return _make_package(service.store, 'stream_a', 1, datetime(2026, 7, 26, tzinfo=timezone.utc))


def _download(service: _Service, package_id: str, who: str, byte_range: str = '') -> bytes:
    headers = {**_auth(who), **({'Range': byte_range} if byte_range else {})}
    response = service.client.get(f'/v1/packages/{package_id}/zip', headers=headers)
    assert response.status_code in (200, 206), response.text
    return response.content


def _receipt(service: _Service, package_id: str, who: str, **body: object):
    return service.client.post(f'/v1/packages/{package_id}/receipt', headers=_auth(who), json=body)


# --- the route census ------------------------------------------------------------------------------

def test_no_route_can_pack_and_every_route_is_on_this_list(service):
    """A new route fails here until somebody has looked at it — the way "no route packs" stays true."""
    routes = sorted((path, method.upper()) for path, methods in service.app.openapi()['paths'].items()
                    if path.startswith('/v1/packages') for method in methods)
    assert routes == sorted([
        ('/v1/packages/health', 'GET'), ('/v1/packages/schedule', 'GET'),
        ('/v1/packages', 'GET'), ('/v1/packages/ledger', 'GET'),
        ('/v1/packages/schedule', 'POST'), ('/v1/packages/schedule/weekly', 'POST'),
        ('/v1/packages/schedule/{special_id}', 'DELETE'),
        ('/v1/packages/{package_id}', 'GET'), ('/v1/packages/{package_id}/zip', 'GET'),
        ('/v1/packages/{package_id}/receipt', 'POST')])


# --- open routes -------------------------------------------------------------------------------------

def test_health_is_open_and_names_a_private_pipeline_only_by_alias(service, package):
    service.store.write_last_run('stream_p', {'source': 'stream_p', 'result': 'packed', 'at': _NOW.isoformat(),
                                              'duration_seconds': 1.0})
    service.store.write_last_run('stream_a', {'source': 'stream_a', 'result': 'nothing_new',
                                              'at': _NOW.isoformat(), 'duration_seconds': 0.2})
    response = service.client.get('/v1/packages/health')

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok' and body['reasons'] == []
    assert set(body['last_runs']) == {'stream_a', 'private-1'}
    assert 'stream_p' not in response.text
    for key in ('started_at', 'next_run', 'next_run_set_aside', 'next_special', 'packing', 'packer', 'heartbeat'):
        assert key in body


def test_the_schedule_view_is_open_and_aliases_private_sources(service):
    service.schedule.add('stream_p', datetime(2026, 7, 29).date(), None, 'operator', _NOW)
    body = service.client.get('/v1/packages/schedule').json()

    assert [entry['source'] for entry in body['special']] == ['private-1']
    assert body['weekly']['weekday'] == 'sun' and len(body['weekly']['next_runs']) == 4


def test_a_failed_last_run_degrades_health(service):
    service.store.write_last_run('stream_a', {'source': 'stream_a', 'result': 'failed', 'at': _NOW.isoformat(),
                                              'duration_seconds': 0.1})
    body = service.client.get('/v1/packages/health').json()
    assert (body['status'], body['reasons']) == ('degraded', ['last_run_failed'])


def test_an_unconfirmed_package_turns_overdue(service):
    _make_package(service.store, 'stream_a', 1, datetime(2026, 7, 5, tzinfo=timezone.utc))
    body = service.client.get('/v1/packages/health').json()
    assert body['reasons'] == ['receipt_overdue']


# --- the list, the manifest, the zip -----------------------------------------------------------------

def test_the_list_needs_a_token_and_a_packages_grant(service, package):
    assert service.client.get('/v1/packages').status_code == 401
    assert service.client.get('/v1/packages', headers=_auth('reports_only')).status_code == 403
    body = service.client.get('/v1/packages', headers=_auth('ide')).json()
    entry = body['packages'][0]
    assert body['count'] == 1 and entry['package_id'] == package[0]
    assert (entry['source'], entry['state'], entry['received_at']) == ('stream_a', 'ready', None)
    assert entry['sha256'] == hashlib.sha256(package[1]).hexdigest() and entry['record_count'] == 2000


def test_the_manifest_carries_the_receipt_field(service, package):
    body = service.client.get(f'/v1/packages/{package[0]}', headers=_auth('ide')).json()
    assert body['receipt'] is None and body['source'] == 'stream_a'
    assert service.client.get('/v1/packages/stream_a-0099-20260726', headers=_auth('ide')).status_code == 404


def test_a_download_is_the_zip_and_is_booked(service, package):
    data = _download(service, package[0], 'ide')
    served = service.ledger.read(event='served').events

    assert data == package[1]
    assert served[0]['consumer'] == 'ide' and served[0]['status'] == 200
    assert served[0]['bytes_sent'] == len(data) and served[0]['complete'] is True


def test_a_range_answers_206_with_that_part(service, package):
    part = _download(service, package[0], 'ide', 'bytes=10-')
    assert part == package[1][10:]
    assert service.ledger.read(event='served').events[0]['status'] == 206


# --- receipts ----------------------------------------------------------------------------------------

def test_the_hash_alone_is_refused(service, package):
    """The list publishes the hash, so holding it proves nothing: a complete download must stand behind it."""
    sha = hashlib.sha256(package[1]).hexdigest()
    response = _receipt(service, package[0], 'ide', zip_sha256=sha)
    assert response.status_code == 409 and 'no complete download' in response.json()['detail']
    assert service.store.find(package[0]).state == 'ready'


def test_a_verified_receipt_delivers_and_a_repeat_returns_the_original(service, package):
    sha = hashlib.sha256(_download(service, package[0], 'ide')).hexdigest()
    first = _receipt(service, package[0], 'ide', zip_sha256=sha, note='2000 imported')
    again = _receipt(service, package[0], 'ide', zip_sha256=sha, note='again')

    assert first.status_code == 201 and first.json()['created'] is True
    assert again.status_code == 200 and again.json()['note'] == '2000 imported'
    found = service.store.find(package[0])
    assert found.state == 'delivered' and found.receipt['zip_sha256'] == sha
    listed = service.client.get('/v1/packages?unacknowledged=true', headers=_auth('ide')).json()
    assert listed['count'] == 0
    assert [e['event'] for e in service.ledger.read(package_id=package[0]).events
            if e['event'] in ('received', 'delivered')] == ['received', 'delivered']


def test_a_wrong_hash_is_not_this_package(service, package):
    _download(service, package[0], 'ide')
    response = _receipt(service, package[0], 'ide', zip_sha256='0' * 64)
    assert response.status_code == 409 and 'not this package' in response.json()['detail']


def test_another_tokens_download_does_not_count(service, package):
    sha = hashlib.sha256(_download(service, package[0], 'other')).hexdigest()
    assert _receipt(service, package[0], 'ide', zip_sha256=sha).status_code == 409


def test_a_resumed_download_adds_up_across_its_attempts(service, package):
    size = len(package[1])
    head = _download(service, package[0], 'ide', f'bytes=0-{size // 2 - 1}')
    tail = _download(service, package[0], 'ide', f'bytes={size // 2}-')
    sha = hashlib.sha256(head + tail).hexdigest()
    assert _receipt(service, package[0], 'ide', zip_sha256=sha).status_code == 201


def test_a_rehearsal_receipt_marks_nothing_and_never_stands_on_a_real_one(service, package):
    sha = hashlib.sha256(_download(service, package[0], 'rehearsal')).hexdigest()
    rehearsal = _receipt(service, package[0], 'rehearsal', zip_sha256=sha)

    assert rehearsal.status_code == 200 and rehearsal.json()['dev'] is True
    assert service.store.find(package[0]).state == 'ready'
    assert _receipt(service, package[0], 'ide', zip_sha256=sha).status_code == 409   # the IDE never downloaded
    _download(service, package[0], 'ide')
    assert _receipt(service, package[0], 'ide', zip_sha256=sha).status_code == 201


def test_a_receipt_without_a_hash_proves_nothing_and_blocks_nothing(service, package):
    """The collector's store let a hashless first receipt take the slot; a hashed one could then no
    longer deliver the package. Here it is booked and changes nothing."""
    data = _download(service, package[0], 'ide')
    hashless = _receipt(service, package[0], 'ide', note='checking')
    assert hashless.status_code == 200 and hashless.json()['verified'] is False
    assert service.store.find(package[0]).state == 'ready'

    hashed = _receipt(service, package[0], 'ide', zip_sha256=hashlib.sha256(data).hexdigest())
    assert hashed.status_code == 201 and service.store.find(package[0]).state == 'delivered'


@pytest.mark.parametrize('body', [{'note': 'x' * 501}, {'note': 7}, {'zip_sha256': 'not-hex'},
                                  {'zip_sha256': 'a' * 63}])
def test_a_malformed_receipt_is_a_400(service, package, body):
    assert _receipt(service, package[0], 'ide', **body).status_code == 400


def test_a_receipt_for_no_package_is_a_404(service):
    assert _receipt(service, 'stream_a-0001-20260726', 'ide', zip_sha256='a' * 64).status_code == 404


# --- schedule changes are the operator's ------------------------------------------------------------------

def test_a_consumer_cannot_order_packages(service):
    response = service.client.post('/v1/packages/schedule', headers=_auth('ide'),
                                   json={'source': 'stream_a', 'cutoff': '2026-07-29'})
    assert response.status_code == 403


def test_the_operator_plans_and_cancels_a_special_run(service):
    added = service.client.post('/v1/packages/schedule', headers=_auth('operator'),
                                json={'source': 'stream_a', 'cutoff': '2026-07-29'})
    assert added.status_code == 201
    entry = added.json()
    assert entry['run_at'] == '2026-07-29T01:00:00+00:00' and entry['state'] == 'planned'
    twice = service.client.post('/v1/packages/schedule', headers=_auth('operator'),
                                json={'source': 'stream_a', 'cutoff': '2026-07-29'})
    assert twice.status_code == 409 and 'already has a run' in twice.json()['detail']
    cancelled = service.client.delete(f"/v1/packages/schedule/{entry['id']}", headers=_auth('operator'))
    assert cancelled.status_code == 200 and cancelled.json()['state'] == 'cancelled'
    events = [e['event'] for e in service.ledger.read().events]
    assert events == ['schedule_added', 'schedule_refused', 'schedule_cancelled']


def test_the_weekly_run_is_set_aside_and_resumed(service):
    response = service.client.post('/v1/packages/schedule/weekly', headers=_auth('operator'),
                                   json={'action': 'set_aside'})
    assert response.status_code == 200
    assert service.schedule.weekly_skipped(datetime(2026, 8, 2, 1, 30, tzinfo=timezone.utc))
    service.client.post('/v1/packages/schedule/weekly', headers=_auth('operator'), json={'action': 'resume'})
    assert not service.schedule.weekly_skipped(datetime(2026, 8, 2, 1, 30, tzinfo=timezone.utc))
