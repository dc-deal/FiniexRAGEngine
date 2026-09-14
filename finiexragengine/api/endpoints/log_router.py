"""`GET /v1/logs/{name}` — the engine's own log over a UTC time range (2026-09-08).

Every other diagnostic became answerable over HTTP with ISSUE_104; the log did not. On 2026-09-08
that was the whole gap: four connectivity outages whose cause was one word inside a traceback
(`getaddrinfo failed`), and reading it meant an RDP session and a copied file while the incident was
still running.

**This widens the exposed surface, deliberately and on the operator's decision.** The mitigations are
all here rather than in an argument: a grant nobody holds by default, no caller-supplied path, a
bounded answer, and redaction that reports itself. `core/observability/log_reader.py` owns the
reading — this file is transport, per the CLI/route convention.

`{name}` exists so the grant model applies unchanged: the surface is declared once on the router and
the *name* is the path parameter, exactly as `reports:<name>` works. One log stream is named today;
a second would need its own name written into a token, which is the point.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Security
from finiex_auth.grant_auth import build_grant_dependency
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.core.observability.log_reader import read_log
from finiexragengine.types.api_types import LogEntryInfo, LogPageResponse
from finiexragengine.utils.dataclass_json import to_jsonable

logger = logging.getLogger(__name__)

# The one stream this engine exposes. A closed set rather than a path: a caller must never be able
# to name a file, which is the difference between a log route and an arbitrary read primitive.
_STREAMS = ('engine',)


def build_log_router(log_file: Optional[str], tokens: TokenRegistry,
                     max_lines: int = 2000) -> APIRouter:
    """Serve a bounded, redacted slice of the engine log.

    `log_file` is the configured path (`logging.file`) and nothing else — the caller picks a
    *range*, never a file. `None` means file logging is off, and the route says so rather than
    pretending an empty log.
    """
    router = APIRouter(prefix='/v1/logs', tags=['logs'],
                       dependencies=[Security(build_grant_dependency(tokens), scopes=['logs'])])

    @router.get('/{name}', response_model=LogPageResponse)
    def read(name: str,
             since: Optional[datetime] = Query(
                 None, description='UTC lower bound; the file is local time, converted for you'),
             until: Optional[datetime] = Query(None, description='UTC upper bound'),
             min_level: str = Query('WARNING', pattern='^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$'),
             limit: int = Query(200, ge=1, le=max_lines)) -> LogPageResponse:
        """Entries in the range, newest first. 404 for an unknown stream, 503 with file logging off.

        `min_level` defaults to WARNING because the file carries thousands of INFO lines a night and
        the question this route answers is always "what went wrong". Widening it is a narrowing
        parameter like any other — it never selects a different resource.
        """
        if name not in _STREAMS:
            raise HTTPException(status_code=404, detail=f'no log stream named {name!r}')
        if not log_file:
            # A configuration fact, not a failure to read: `logging.file: null` is a supported mode.
            raise HTTPException(status_code=503,
                                detail='file logging is disabled (logging.file is null)')
        page = read_log(Path(log_file), since=since, until=until,
                        min_level=min_level, limit=limit)
        return LogPageResponse(
            stream=name, since=page.since, until=page.until, min_level=page.min_level,
            matched=page.matched, truncated=page.truncated,
            redacted_lines=page.redacted_lines, files_read=page.files_read,
            entries=[LogEntryInfo(**to_jsonable(entry)) for entry in page.entries])

    return router
