"""`/v1/packages…` — signal packages: list, manifest, zip, receipt; open health and schedule.

The contract is the collector's tick packages' (testingide#120): the same routes, fields and answers,
so the Testing IDE fetches both services with one client. What differs is said where it differs:
`source` instead of `collector`, `record_count` instead of `tick_count`, members named
`<pipeline_id>_<YYYY-MM-DD>.jsonl`.

**No route packs.** The routes read what a package run wrote — small JSON files and the zip — and
parse nothing large; a run is a child process the schedule starts (`package_loop`). The route census
in the tests keeps it that way.

**Gates.** `packages:<id>` for a manifest and its zip, `receipts:<id>` for a receipt,
`package_schedule` for schedule changes. The list and the ledger have no identity segment, so they
need any `packages` grant and are filtered per token. Health and the schedule's view are open: they
describe the service, and private pipelines appear there only under their public alias
(`private-1`, …), as on `/v1/health`.

**Receipts**, checked in the collector's order: 400 for a bad note, a missing hash ("zip_sha256 is
required", booked as `receipt_refused`) or a malformed one; 404 for no such package; 409 when the hash
is not this package's; 409 when the same token has not downloaded the whole zip (the hash is public,
so it proves nothing alone — a resumed download adds up across its attempts); a token in
`packages.dev_consumers` rehearses (`dev_received`, nothing moves); otherwise 201 for the first
verified receipt and 200 with the original for a repeat. The package then moves to `delivered`. A
receipt without the hash never reaches the slot, so it cannot block a verified one later and the
package stays in `?unacknowledged=true` (aligned with the collector, 2026-11-13).
"""
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException, Query, Request, Security
from fastapi.responses import FileResponse, JSONResponse
from finiex_auth.grant_auth import build_grant_dependency
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.api.ledgered_file_response import DownloadOutcome, LedgeredFileResponse
from finiexragengine.core.packages.package_health import package_health
from finiexragengine.core.packages.package_ledger import PackageLedger, downloaded_whole
from finiexragengine.core.packages.package_schedule import PackageSchedule, ScheduleRefused
from finiexragengine.core.packages.package_store import PackageStore
from finiexragengine.types.config_types.app_config_types import PackagesConfig

NOTE_MAX_CHARS = 500
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def build_package_routers(*, store: PackageStore, ledger: PackageLedger, schedule: PackageSchedule,
                          config: PackagesConfig, tokens: TokenRegistry,
                          loop_status: Callable[[], Dict[str, Any]],
                          heartbeat_status: Callable[[], Dict[str, Any]],
                          packer: Dict[str, Any], public_names: Dict[str, str],
                          clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                          ) -> Tuple[APIRouter, APIRouter]:
    """`(open router, protected router)`. `public_names` maps a private pipeline id to its alias."""
    grant = build_grant_dependency(tokens)

    def public(name: str) -> str:
        return public_names.get(name, name)

    def consumer_of(request: Request) -> Optional[str]:
        return getattr(request.state, 'consumer', None)

    def may(request: Request, grant_name: str) -> bool:
        consumer = consumer_of(request)
        return consumer is None or tokens.may(consumer, grant_name)

    # --- open: what the service is and when it runs --------------------------------------------

    public_router = APIRouter(prefix='/v1/packages', tags=['packages'])

    @public_router.get('/health')
    def health() -> Dict[str, Any]:
        """The package service's verdict and clock, open for a consumer's park check."""
        verdict = package_health(store, config, clock())
        return {'status': verdict['status'], 'reasons': verdict['reasons'], **loop_status(),
                'packer': packer, 'heartbeat': heartbeat_status(),
                'last_runs': {public(source): {key: record.get(key)
                                               for key in ('result', 'at', 'duration_seconds')}
                              for source, record in store.last_runs().items()}}

    @public_router.get('/schedule')
    def schedule_view() -> Dict[str, Any]:
        """The weekly runs ahead, the special runs, and the limits."""
        view = schedule.view(clock())
        for entry in view['special']:
            entry['source'] = public(entry['source'])
        return view

    # --- protected: the packages themselves ----------------------------------------------------

    router = APIRouter(prefix='/v1/packages', tags=['packages'])

    @router.get('', dependencies=[Security(grant, scopes=['packages'])])
    def list_packages(request: Request,
                      source: Optional[str] = Query(None, description='only this source (pipeline id)'),
                      unacknowledged: bool = Query(False, description='only packages without a receipt'),
                      ) -> Dict[str, Any]:
        """Every package this token may fetch, oldest first."""
        entries = []
        for manifest in store.all_manifests():
            if not may(request, f"packages:{manifest['package_id']}"):
                continue
            if source is not None and manifest['source'] != source:
                continue
            found = store.find(manifest['package_id'])
            if found is None or (unacknowledged and found.receipt is not None):
                continue
            entries.append(_summary(manifest, found.state, found.receipt))
        everything = may(request, 'packages:*')
        return {'count': len(entries), 'packages': entries,
                'health': package_health(store, config, clock()),
                'last_runs': store.last_runs() if everything else {}, **loop_status()}

    @router.get('/ledger', dependencies=[Security(grant, scopes=['packages'])])
    def read_ledger(request: Request, package_id: Optional[str] = Query(None),
                    event: Optional[str] = Query(None)) -> Dict[str, Any]:
        """Every step of the packages this token may fetch; schedule events need `packages:*`."""
        read = ledger.read(package_id=package_id, event=event)
        events = [record for record in read.events
                  if may(request, f"packages:{record['package_id']}" if record.get('package_id')
                         else 'packages:*')]
        return {'count': len(events), 'unreadable_lines': read.unreadable_lines, 'events': events}

    @router.post('/schedule', status_code=201, dependencies=[Security(grant, scopes=['package_schedule'])])
    def add_special(request: Request, body: Optional[Dict[str, Any]] = Body(None)) -> Dict[str, Any]:
        """Plan a special run: `{"source": "<pipeline_id>|all", "cutoff": "YYYY-MM-DD", "run_at"?: ISO}`."""
        body = body or {}
        source = body.get('source')
        if not isinstance(source, str) or not source:
            raise HTTPException(status_code=400, detail="'source' is required: a pipeline id or 'all'")
        cutoff = _day(body.get('cutoff'), 'cutoff')
        run_at = _moment(body.get('run_at'), 'run_at') if body.get('run_at') is not None else None
        try:
            entry = schedule.add(source, cutoff, run_at, consumer_of(request), clock())
        except ScheduleRefused as exc:
            ledger.append('schedule_refused', consumer=consumer_of(request), source=source,
                          cutoff=cutoff.isoformat(), reason=str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ledger.append('schedule_added', consumer=consumer_of(request), special_id=entry['id'],
                      source=entry['source'], cutoff=entry['cutoff'], run_at=entry['run_at'])
        return entry

    @router.post('/schedule/weekly', dependencies=[Security(grant, scopes=['package_schedule'])])
    def weekly(request: Request, body: Optional[Dict[str, Any]] = Body(None)) -> Dict[str, Any]:
        """`{"action": "set_aside", "until"?: "YYYY-MM-DD"}` or `{"action": "resume"}`."""
        body = body or {}
        action = body.get('action')
        if action == 'resume':
            schedule.resume()
            ledger.append('schedule_weekly_resumed', consumer=consumer_of(request))
            return {'weekly': 'resumed'}
        if action != 'set_aside':
            raise HTTPException(status_code=400, detail="'action' is 'set_aside' or 'resume'")
        until = _day(body.get('until'), 'until') if body.get('until') is not None else None
        try:
            weekly_state = schedule.set_aside(until, clock(), consumer_of(request))
        except ScheduleRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ledger.append('schedule_weekly_set_aside', consumer=consumer_of(request), **weekly_state)
        return {'weekly': weekly_state}

    @router.delete('/schedule/{special_id}', dependencies=[Security(grant, scopes=['package_schedule'])])
    def cancel_special(request: Request, special_id: str) -> Dict[str, Any]:
        entry = schedule.cancel(special_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f'no planned special run {special_id!r}')
        ledger.append('schedule_cancelled', consumer=consumer_of(request), special_id=special_id,
                      source=entry['source'], cutoff=entry['cutoff'])
        return entry

    @router.get('/{package_id}', dependencies=[Security(grant, scopes=['packages'])])
    def manifest(package_id: str) -> Dict[str, Any]:
        """The manifest, plus `receipt` (null until a verified one arrives)."""
        found = store.find(package_id)
        if found is None:
            raise HTTPException(status_code=404, detail='no such package')
        return {**found.manifest, 'receipt': found.receipt}

    # `response_class=FileResponse` documents the route; the handler returns the counting subclass.
    @router.get('/{package_id}/zip', response_class=FileResponse,
                dependencies=[Security(grant, scopes=['packages'])])
    def download(request: Request, package_id: str) -> LedgeredFileResponse:
        """The zip, unchanged for as long as it is listed; `Range` answers 206. Every download is
        booked in the ledger when it ends, with what it really handed over."""
        found = store.find(package_id)
        if found is None or not found.zip_path.is_file():
            raise HTTPException(status_code=404, detail='no such package')
        consumer, requested = consumer_of(request), request.headers.get('range')

        def served(outcome: DownloadOutcome) -> None:
            ledger.append('served', package_id=package_id, consumer=consumer, range=requested, **outcome)

        return LedgeredFileResponse(found.zip_path, on_finish=served, media_type='application/zip',
                                    filename=found.zip_path.name)

    @router.post('/{package_id}/receipt', dependencies=[Security(grant, scopes=['receipts'])])
    def receipt(request: Request, package_id: str,
                body: Optional[Dict[str, Any]] = Body(None)) -> JSONResponse:
        """`{"zip_sha256": "<hex>", "note": "…"}` once the zip is verified, imported and filed."""
        body = body or {}
        note = body.get('note', '')
        if not isinstance(note, str) or len(note) > NOTE_MAX_CHARS:
            raise HTTPException(status_code=400, detail=f"'note' is text of at most {NOTE_MAX_CHARS} characters")
        reported = body.get('zip_sha256')
        consumer = consumer_of(request)
        if reported is None:
            # Required, as the collector answers it (2026-11-13): a receipt without the hash proves nothing,
            # so it is refused and booked — it never takes the slot, and the package stays unacknowledged.
            ledger.append('receipt_refused', package_id=package_id, consumer=consumer, reason='no zip_sha256')
            raise HTTPException(status_code=400, detail='zip_sha256 is required')
        if not isinstance(reported, str) or not _SHA256.match(reported.lower()):
            raise HTTPException(status_code=400, detail="'zip_sha256' is 64 hexadecimal characters")
        reported = reported.lower()
        found = store.find(package_id)
        if found is None:
            raise HTTPException(status_code=404, detail='no such package')
        expected = found.manifest['zip']['sha256']
        if reported != expected:
            ledger.append('receipt_refused', package_id=package_id, consumer=consumer,
                          reported_sha256=reported, expected_sha256=expected)
            raise HTTPException(status_code=409, detail='what you hold is not this package')
        dev = consumer is not None and consumer in config.dev_consumers
        served_events = ledger.read(package_id=package_id, event='served').events
        if (dev or found.receipt is None) and not downloaded_whole(
                served_events, consumer, int(found.manifest['zip']['size_bytes'])):
            ledger.append('receipt_refused', package_id=package_id, consumer=consumer,
                          reported_sha256=reported, reason='no complete download by this token')
            raise HTTPException(status_code=409, detail='no complete download of this zip by this token')
        if dev:
            ledger.append('dev_received', package_id=package_id, consumer=consumer, note=note,
                          zip_sha256=reported, verified=True)
            return JSONResponse({'package_id': package_id, 'dev': True, 'created': False,
                                 'detail': 'rehearsal: every check passed, nothing was marked'})
        record = {'package_id': package_id, 'received_at': clock().isoformat(), 'consumer': consumer,
                  'note': note, 'zip_sha256': reported}
        written, created = store.write_receipt(package_id, record)
        if written is None:
            raise HTTPException(status_code=404, detail='no such package')
        if created:
            ledger.append('received', package_id=package_id, consumer=consumer, note=note,
                          zip_sha256=reported, verified=True)
            if store.deliver(package_id):
                ledger.append('delivered', package_id=package_id, source=found.source)
        return JSONResponse({**written, 'created': created}, status_code=201 if created else 200)

    return public_router, router


def public_package_names(private_pipeline_ids: List[str]) -> Dict[str, str]:
    """Private pipeline id → `private-N`, numbered in sorted order like `/v1/health`'s aliases."""
    return {pipeline_id: f'private-{number}'
            for number, pipeline_id in enumerate(sorted(private_pipeline_ids), start=1)}


def _summary(manifest: Dict[str, Any], state: str, receipt: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A list entry: the collector's, with `source` and `record_count`."""
    return {'package_id': manifest['package_id'], 'source': manifest['source'],
            'instance_id': manifest.get('instance_id'), 'cutoff': manifest['cutoff'],
            'previous_package_id': manifest.get('previous_package_id'),
            'created_at': manifest['created_at'], 'member_count': manifest['member_count'],
            'record_count': manifest['record_count'], 'size_bytes': manifest['zip']['size_bytes'],
            'sha256': manifest['zip']['sha256'],
            'received_at': receipt.get('received_at') if receipt else None, 'state': state}


def _day(value: Any, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"'{label}' is a day YYYY-MM-DD") from None


def _moment(value: Any, label: str) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"'{label}' is an ISO time with a zone") from None
    if moment.tzinfo is None:
        raise HTTPException(status_code=400, detail=f"'{label}' needs a zone (UTC expected)")
    return moment

