"""`GET /v1/reports` — the diagnostic surface over the report catalog (ISSUE_104).

Mounted on the **protected** router, so every entry requires a token and one added later inherits
that by construction (ISSUE_98). Nothing here decides *what* a report contains: the catalog owns the
builders and the config resolution, this file owns the transport — parameter parsing, the window
ceiling, and the shape of the answer.

**Read-only by construction.** The catalog has no entry that can spend; see its module docstring for
why `coverage` is absent rather than merely last in the queue.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Security

from finiexragengine.api.grant_auth import build_grant_dependency
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports import report_catalog
from finiexragengine.exceptions.ragengine_errors import FiniexRagError
from finiexragengine.types.api_types import (
    AppliedParamInfo,
    ReportCatalog,
    ReportCatalogEntry,
    ReportEnvelope,
)
from finiexragengine.types.report_types import ResolvedReport
from finiexragengine.utils.dataclass_json import to_jsonable

logger = logging.getLogger(__name__)


def build_report_router(database_url: str, config_manager: AppConfigManager,
                        tokens: TokenRegistry, max_window_days: int = 90) -> APIRouter:
    """Serve the catalog and one report at a time.

    `max_window_days` bounds every window: an unbounded one is a full scan of the journal, and the
    caller is a diagnostic tool that will happily ask for `all` on a table that grows for years.

    `tokens` answers the second question this surface raises: a verified consumer is not
    automatically entitled to *every* report. Spend belongs to the operator, feed diagnostics may
    belong to a collector — so each token declares what it reads, and both the listing and the
    fetch honour it (ISSUE_104).
    """

    def _permitted(request: Request, name: str) -> bool:
        """Whether this caller may read `name` — for FILTERING the listing only.

        The *gate* is `grant_auth`, mounted on the router: it derives `reports:<name>` from the
        matched route and answers 403. This exists because a collection route has no identity
        segment and therefore cannot be gated — it is filtered instead, so a caller entitled to
        some reports still gets a useful answer rather than a refusal.
        """
        consumer = getattr(request.state, 'consumer', None)
        return consumer is None or tokens.may(consumer, f'reports:{name}')
    # `scopes=['reports']` is the SURFACE half of every grant on this router; the name half is the
    # route's path parameter. Declared here, once, so a report added to the catalog is gated
    # without touching a route (ISSUE_104).
    router = APIRouter(prefix='/v1/reports', tags=['reports'],
                       dependencies=[Security(build_grant_dependency(tokens), scopes=['reports'])])

    def _clamp(resolved: ResolvedReport) -> None:
        """Shorten a window that reaches past the ceiling, and say so.

        Clamping rather than refusing: a diagnostic caller asking for `all` wants as much as it can
        have, not an error. The ceiling is a property of the *exposed* surface — the console has
        none, because an operator at the machine may ask for anything — so it lives here and not in
        the catalog both surfaces share.
        """
        if resolved.params.since is None:
            return
        ceiling = datetime.now(timezone.utc) - timedelta(days=max_window_days)
        if resolved.params.since >= ceiling:
            return
        resolved.params.since = ceiling
        resolved.params.window_label = f'{max_window_days}d'
        window = resolved.applied.get('window')
        if window is not None:
            window.value = f'{max_window_days}d'
            window.clamped = True

    @router.get('', response_model=ReportCatalog)
    def catalog(request: Request) -> ReportCatalog:
        """The reports **this caller** can produce, with the parameters each one accepts.

        Filtered rather than complete: a listing that advertised reports the caller cannot fetch
        would turn every scope into a discovery of a 403.
        """
        return ReportCatalog(
            reports=[ReportCatalogEntry(**vars(entry))
                     for entry in report_catalog.list_reports(
                         config_manager.get_config().reports)
                     if _permitted(request, entry.name)],
            max_window_days=max_window_days)

    @router.get('/{name}', response_model=ReportEnvelope)
    def report(request: Request, name: str,
               window: Optional[str] = Query(None, description="e.g. '7d', '30', 'all'"),
               source_id: Optional[str] = Query(None),
               episode_start: Optional[datetime] = Query(None),
               symbol: Optional[str] = Query(None, description='narrow a per-symbol series'),
               recent_problems: Optional[int] = Query(None, ge=1, le=200),
               recent_passes: Optional[int] = Query(None, ge=1, le=500)
               ) -> ReportEnvelope:
        """Build one report. 404 for an unknown name, 422 for a parameter it cannot use."""
        try:
            spec = report_catalog.get_spec(name)
        except KeyError:
            # A caller error, not a failure to produce — and never a 500, which would read as
            # "the report is broken" rather than "there is no such report".
            raise HTTPException(status_code=404, detail=f'no report named {name!r}')

        supplied = {'source_id': source_id, 'episode_start': episode_start, 'symbol': symbol,
                    'window': window, 'recent_problems': recent_problems,
                    'recent_passes': recent_passes}
        missing = [param for param in spec.required if not supplied.get(param)]
        if missing:
            raise HTTPException(status_code=422,
                                detail=f'{name} requires: {", ".join(missing)}')
        unusable = [key for key, value in supplied.items()
                    if value is not None and key not in spec.params]
        if unusable:
            # Accepting a parameter and then ignoring it is the failure this whole provenance
            # model exists to prevent — so an unusable one is refused rather than dropped.
            raise HTTPException(
                status_code=422,
                detail=f'{name} does not take: {", ".join(sorted(unusable))} '
                       f'(it takes: {", ".join(spec.params) or "no parameters"})')

        try:
            resolved = report_catalog.resolve(name, config_manager.get_config().reports, supplied)
        except (ValueError, IndexError):
            raise HTTPException(status_code=422,
                                detail=f'window must look like 7d, 30, or all — got {window!r}')
        _clamp(resolved)

        try:
            built = report_catalog.build_report(name, database_url, config_manager,
                                                resolved.params)
        except FiniexRagError as exc:
            # The store could not be read. That is an availability problem, not a bug in the
            # request, and a diagnostic caller needs to tell the two apart.
            logger.warning('[REPORTS] %s could not be built: %s', name, exc)
            raise HTTPException(status_code=503, detail=f'report {name} unavailable: {exc}')
        return ReportEnvelope(
            report=name, generated_at=datetime.now(timezone.utc),
            params={key: AppliedParamInfo(**vars(param))
                    for key, param in resolved.applied.items()},
            since=resolved.params.since, data=to_jsonable(built))

    return router
