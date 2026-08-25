"""The grant check (ISSUE_104) — FastAPI's own scope mechanism, plus the route's identity.

`bearer_auth` answers *who is calling*; this answers *may they reach this*. Both are router-level
dependencies, so a route added later is authenticated and authorised by construction rather than by
someone remembering a decorator.

**A grant is `<surface>:<name>`, and both halves come from FastAPI itself.**

- the **surface** is declared where the router is built — `Security(dependency, scopes=['reports'])`
  — which is FastAPI's `SecurityScopes`, the mechanism it provides for exactly this. Declared for
  authorization rather than inferred from a path or borrowed from documentation tags, so a `/v2`
  prefix or a rename cannot move it, and it appears in the OpenAPI security metadata;
- the **name** is the route's first path parameter, which already *is* a domain identifier:
  `/v1/reports/{name}` carries the report id, `/v1/pipelines/{pipeline_id}/latest` the pipeline id.

So a grant names a **thing**, never an address, and the comparison is exact — no wildcard matching
against caller-supplied paths, which is where authorization defects live.

A **collection** route (`/v1/reports`, `/v1/pipelines`) has no identity segment and is not gated
here: it is *filtered* in its handler to what the caller holds. Gating it would answer 403 to a
consumer entitled to some of what it lists.
"""
import logging
from typing import Callable, Optional

from fastapi import HTTPException, Request
from fastapi.security import SecurityScopes

from finiexragengine.api.token_registry import TokenRegistry

logger = logging.getLogger(__name__)


def route_identity(request: Request) -> Optional[str]:
    """The first path parameter of the matched route — the thing being addressed.

    First rather than last: a nested route (`/v1/reports/{name}/rows/{row_id}`) is governed by what
    it belongs to, which is what a grant is about — one is entitled to a report, not to one of its
    rows.
    """
    route = request.scope.get('route')
    template = getattr(route, 'path', '') if route is not None else ''
    params = request.scope.get('path_params') or {}
    for segment in template.strip('/').split('/'):
        if segment.startswith('{') and segment.endswith('}'):
            name = segment[1:-1]
            if name in params:
                return str(params[name])
    return None


def build_grant_dependency(tokens: TokenRegistry) -> Callable[..., None]:
    """The dependency mounted with `Security(..., scopes=['<surface>'])` on a domain router."""

    def require_grant(security_scopes: SecurityScopes, request: Request) -> None:
        consumer = getattr(request.state, 'consumer', None)
        if consumer is None:
            # Authentication is off (scaffold mode, contract tests). There is no consumer to hold a
            # grant, and an engine configured that way has already decided it is not exposed — it
            # refuses to boot in any other configuration.
            return
        if not security_scopes.scopes:
            return
        identity = route_identity(request)
        if identity is None:
            return
        grant = f'{security_scopes.scopes[0]}:{identity}'
        if not tokens.may(consumer, grant):
            logger.warning('[AUTH] %s denied %s', consumer, grant)
            # 403 rather than 404: the thing exists, a partner can read the documentation anyway,
            # and a denial they can debug beats one they have to guess at.
            raise HTTPException(
                status_code=403,
                detail=f'token {consumer!r} does not hold {grant!r} · holds: '
                       f'{tokens.grants_of(consumer)}')

    return require_grant
