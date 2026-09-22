"""`GET /v1/dashboard/{name}` — what the live console would be showing, for a viewer elsewhere.

Since 2026-09-22 the engine runs as a Windows service with no console: a console it shared was a
console that could suspend it, and drawing on that console put up to 3.9 s of Rich rendering inside
the loop that timestamps the data. The dashboard did not become less useful, it became unreachable —
so it moves to a separate process on another machine, and this is the seam it reads.

**Its own address rather than a field on `/v1/health`.** `/health` answers one question — is the
process alive and are the workers turning — and `docs/architecture/health_contract.md` names the
fields a consumer already derives behaviour from. Hanging a panel payload off it would put every
future rendering change inside another project's contract, and it would make one route answer two
questions (CLAUDE.md: one report, one command, one route).

**`{name}` is an identity segment over a closed set**, exactly as the feed doctor's `{name}` is. Not
decoration: a collection route has no path parameter, so the grant model can only apply its surface
floor to it, and the scope walk in `tests/api/test_report_scopes.py` — which only walks paths
containing a `{` — would never see it. The segment is what keeps both mechanisms working.

Transport only: `core/ui/dashboard_snapshot.py` owns what a reading contains, and the provider is
built once where the collaborators live.
"""
import logging
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Security
from finiex_auth.grant_auth import build_grant_dependency
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.core.ui.dashboard_snapshot import DASHBOARD_VIEWS, DashboardSnapshot
from finiexragengine.types.api_types import DashboardResponse
from finiexragengine.utils.dataclass_json import to_jsonable

logger = logging.getLogger(__name__)


def build_dashboard_router(tokens: TokenRegistry,
                           snapshot_provider: Optional[Callable[[], DashboardSnapshot]] = None,
                           ) -> APIRouter:
    """Serve one reading of the engine's live state.

    `snapshot_provider` is `None` when this process holds no live state — an API-only boot, or
    scaffold-mock mode. The route then answers **503 with the reason** rather than an empty panel: a
    viewer that draws zeros because nothing is collecting looks exactly like an engine where nothing
    is happening, and those are different facts.
    """
    router = APIRouter(prefix='/v1/dashboard', tags=['dashboard'],
                       dependencies=[Security(build_grant_dependency(tokens),
                                              scopes=['dashboard'])])

    @router.get('/{name}', response_model=DashboardResponse)
    def dashboard(name: str) -> DashboardResponse:
        """One view of the live state. 404 for an unknown view, 503 when nothing is collecting."""
        if name not in DASHBOARD_VIEWS:
            raise HTTPException(status_code=404, detail=f'no dashboard view named {name!r}')
        if snapshot_provider is None:
            raise HTTPException(
                status_code=503,
                detail='this process holds no live state — it runs without workers, so nothing '
                       'is collecting. Start the engine with --workers to make the view answerable.')
        snapshot = snapshot_provider()
        # The header is the contract and stays typed; the state travels structurally, so a
        # measurement added to a stage snapshot reaches a viewer with no converter edit here.
        return DashboardResponse(view=snapshot.view,
                                 snapshot_at=snapshot.snapshot_at,
                                 version=snapshot.version,
                                 engine_started_at=snapshot.engine_started_at,
                                 journal_named=snapshot.journal_named,
                                 state=to_jsonable(snapshot.state))

    return router
