"""`GET /v1/configs/{name}` — the effective configuration of this process (2026-09-08).

The assistant works in the dev container; the engine runs on the server. The one layer that differs
between them is the one nothing exposed: `user_configs/` is gitignored, so which feeds a machine has
switched off, which model variant is disabled and which thresholds it actually runs were readable
only over RDP. The `[OVERRIDE]` boot line is a notice, not an answer — capped at six leaves, and it
never prints a string value.

**This file is transport.** The projection, the redaction and the census live in
`configuration/abstract_config_view.py`, where no caller can skip them — a config document is
exactly the kind of payload where "the router remembered to sanitize" is not a property worth
resting on. Same split as `report_router` / `report_catalog`.

`{name}` is a closed set of three, so the grant model applies unchanged: the surface is declared
once on the router and the name is the path parameter, exactly as `reports:<name>` works. Three
names rather than one per pipeline keeps pipeline ids and source-set ids out of a single namespace
where they could collide; `?id=` narrows within a document and never selects a different one.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Security

from finiexragengine.api.grant_auth import build_grant_dependency
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.configuration.abstract_config_view import AbstractConfigView
from finiexragengine.types.api_types import (
    ConfigCatalog,
    ConfigCatalogEntry,
    ConfigDocumentResponse,
    OverrideInfo,
)

logger = logging.getLogger(__name__)


def build_config_router(views: Dict[str, AbstractConfigView],
                        tokens: TokenRegistry) -> APIRouter:
    """Serve the config views this process holds, gated by `configs:<name>`.

    `views` are constructed at boot from the objects the engine runs on — never re-read per
    request. A file edited after startup must not make this surface disagree with the running
    engine, the same reason `/v1/build` samples its commit once.
    """
    def _permitted(request: Request, name: str) -> bool:
        """For FILTERING the catalog; the gate on `/{name}` is `grant_auth` at the router."""
        consumer = getattr(request.state, 'consumer', None)
        return consumer is None or tokens.may(consumer, f'configs:{name}')

    router = APIRouter(prefix='/v1/configs', tags=['configs'],
                       dependencies=[Security(build_grant_dependency(tokens),
                                              scopes=['configs'])])

    @router.get('', response_model=ConfigCatalog)
    def catalog(request: Request) -> ConfigCatalog:
        """The documents **this caller** may read, with the ids each one accepts.

        Filtered rather than complete, for the same reason the report catalog is: a listing that
        advertised what the caller cannot fetch turns every scope into a discovery of a 403.
        """
        return ConfigCatalog(configs=[
            ConfigCatalogEntry(name=view.NAME, summary=view.SUMMARY,
                               layers=view.layers(), ids=sorted(view.documents()))
            for name, view in views.items() if _permitted(request, name)])

    @router.get('/{name}', response_model=ConfigDocumentResponse)
    def read(name: str,
             id: Optional[str] = Query(   # noqa: A002 — the path grammar's own word for it
                 None, description='narrow to one pipeline / source set / the app document')
             ) -> ConfigDocumentResponse:
        """One config document, effective and redacted. 404 for an unknown name or id.

        A caller who reached this far holds `configs:<name>`, so an unknown *id* may honestly be a
        404: absence is only informative to someone entitled to the thing that is absent. An
        unknown *name* is a 403 first, decided by the grant dependency above — authorisation before
        resolution, so the endpoint is not an existence oracle.
        """
        view = views.get(name)
        if view is None:
            raise HTTPException(status_code=404, detail=f'no config document named {name!r}')
        document = view.render(id)
        if document is None:
            raise HTTPException(status_code=404,
                                detail=f'{name} has no entry {id!r}')
        return ConfigDocumentResponse(
            name=document.name, summary=document.summary,
            generated_at=datetime.now(timezone.utc), layers=document.layers,
            documents=document.documents,
            overrides={key: [OverrideInfo(**vars(leaf)) for leaf in leaves]
                       for key, leaves in document.overrides.items()},
            redacted=document.redacted, unclassified=document.unclassified,
            scrubbed=document.scrubbed)

    return router
