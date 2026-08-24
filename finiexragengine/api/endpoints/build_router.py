"""`GET /v1/build` — which code this process is running (deploy verification).

Deliberately not a field on `/health`. Health describes **state**: it changes every second, an
uptime probe polls it on an interval, and its payload is a contract the consumer reads. Build
identity is **constant** for the process's lifetime, so it neither belongs in that payload nor
needs to be re-sent with every probe — and keeping it out leaves the health contract untouched.

The value is sampled once, at startup, and handed to this router. Nothing here shells out per
request: see `core/observability/build_info.py` for why that distinction is the point.
"""
from fastapi import APIRouter

from finiexragengine.types.api_types import BuildInfo


def build_build_router(info: BuildInfo) -> APIRouter:
    """Serve one pre-sampled `BuildInfo`, unchanged for as long as this process lives."""
    router = APIRouter(prefix='/v1', tags=['build'])

    @router.get('/build', response_model=BuildInfo)
    def build() -> BuildInfo:
        """The code identity of the running process — version, commit, dirty flag, start time."""
        return info

    return router
