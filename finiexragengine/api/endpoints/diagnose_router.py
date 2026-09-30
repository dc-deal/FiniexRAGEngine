"""`GET /v1/diagnose/{name}` — the feed doctor, from where the feed actually fails (2026-09-09).

On 2026-09-09 `boj_press` failed to parse with `not well-formed (invalid token)` at line 11
column 69. Fetched from the dev container the same feed was clean — 318 lines, 14,722 bytes, and
line 11 only 51 characters long, so **there is no column 69 there**. The server was receiving a
different document, and nothing reachable from here could say which. The instrument that answers
it (`feed_doctor`, ISSUE_11 — raw bytes plus the same feedparser path the ingest worker takes) ran
only on the machine, so the conclusion drawn from here was wrong twice: about the cause, and about
the method.

**This is the first route that reaches *outward* on request.** Every other one reads the store or a
local file, and that difference is worth naming rather than inheriting quietly. What keeps it a
diagnostic rather than an open proxy is all here:

- **`source_id` is required and resolved against the configured catalogue.** A caller names a feed
  this engine already polls; it can never name a URL. The CLI's default is *all* feeds — 39 of them
  at two requests each — and over HTTP that shape would be a 78-request amplifier that also
  perturbs the very feeds whose health it reports on. Here one call is one feed is two requests,
  which is less than the engine's own 15-second poll already costs.
- **A deadline of 10 s**, not the unit's default of 20: a sync endpoint runs in the pool Starlette
  serves every other `def` route from, so the worst case has to stay bounded.
- **A grant nobody holds by default**, exactly like `logs` and `configs`.
- **Redaction on the way out**, because `head` carries bytes a remote host wrote.

Transport only, per the CLI/route convention: `core/sources/feed_doctor.py` owns the diagnosis.

**The second probe, `memory` (2026-09-30), reaches nothing outside the process.** It counts what the
engine holds — GC generations and their pauses, top object types, the objects that leaked before,
and tracemalloc growth when enabled — the instrument that would have named the 7 GB growth of
2026-09-27..30 in minutes instead of a day of reading code. `core/observability/memory_census.py`
owns it. Its cost is the walk over every live object, which holds the GIL; the response states it
(`census_ms`), and the grant (`diagnose:memory`) is held by nobody by default, like `diagnose:feed`.

Both probes share this one route because a grant names the route's first path parameter: a
separate `/v1/diagnose/memory` would have no identity segment and fall back to the surface floor, so
a token holding only `diagnose:feed` could reach it. The price is that `source_id` can no longer be
required by signature — so the feed probe refuses its absence explicitly, and still never means
"all".
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Union

from fastapi import APIRouter, HTTPException, Query, Security
from finiex_auth.grant_auth import build_grant_dependency
from finiex_auth.redaction import redact
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.observability.memory_census import MemoryCensus
from finiexragengine.core.sources.feed_doctor import diagnose_feed
from finiexragengine.types.api_types import FeedDiagnosisResponse, MemoryDiagnosisResponse
from finiexragengine.types.config_types.source_set_types import SourceConfig
from finiexragengine.utils.dataclass_json import to_jsonable

logger = logging.getLogger(__name__)

# The probes this engine exposes. A closed set rather than a free name, so the grant model applies
# unchanged (`diagnose:feed`, `diagnose:memory`) and a caller cannot reach anything not named here.
_PROBES = ('feed', 'memory')

# Fields of `FeedDiagnosis` carrying text this engine did not write. `head` is the whole point of
# the route — the first bytes of the remote body — and therefore arbitrary remote content; the
# other three quote the URL, which can carry a feed's key in its query string.
_SCRUBBED: Tuple[str, ...] = ('head', 'url', 'bozo_exception', 'transport_error')

# Half the unit's own default. See the module docstring: this runs in the pool that serves every
# other sync endpoint, so two sequential probes have to stay a bounded wait.
_TIMEOUT_SECONDS = 10


def build_diagnose_router(source_sets: Optional[SourceSetRegistry],
                          tokens: TokenRegistry,
                          memory_census: Optional[MemoryCensus] = None) -> APIRouter:
    """Probe one configured feed — or this process's memory — and return what the diagnosis found.

    `source_sets` is the registry this process loaded — the same one the ingest workers poll from,
    so a diagnosis is about a feed the engine actually has. `None` is scaffold-mock mode (no
    database, so no catalogue was built) and the route says so rather than answering for a feed
    list it never loaded. `memory_census` is the census this process installed; `None` answers the
    memory probe with 503 rather than with numbers nobody collected.
    """
    router = APIRouter(prefix='/v1/diagnose', tags=['diagnose'],
                       dependencies=[Security(build_grant_dependency(tokens),
                                              scopes=['diagnose'])])

    def _feeds() -> Dict[str, SourceConfig]:
        """Every RSS source across every set, keyed by id — disabled ones included.

        Deliberately not `active_sources()`: the doctor is how an operator checks whether a
        switched-off feed has become reachable again, which is exactly a question about a feed that
        is *not* being polled. The CLI keeps them for the same reason.
        """
        if source_sets is None:
            return {}
        return {source.source_id: source
                for source_set in source_sets.list_sets()
                for source in source_set.sources
                if source.type == 'rss'}

    @router.get('/{name}', response_model=Union[FeedDiagnosisResponse, MemoryDiagnosisResponse])
    def diagnose(name: str,
                 source_id: Optional[str] = Query(
                     None, description='feed probe only: the configured feed to probe — '
                                       'required there, never "all"')
                 ) -> Union[FeedDiagnosisResponse, MemoryDiagnosisResponse]:
        """One probe. 404 for an unknown probe or feed, 503 when its collaborator is absent.

        For `feed`, `source_id` is required. An omitted parameter answering "every feed" is the
        difference between a diagnostic and an amplifier, so its absence is refused with 422 —
        explicitly here, because the signature can no longer require it (see module docstring).
        """
        if name not in _PROBES:
            raise HTTPException(status_code=404, detail=f'no probe named {name!r}')
        if name == 'memory':
            return _memory()
        if not source_id:
            raise HTTPException(status_code=422,
                                detail='source_id is required for the feed probe — one '
                                       'configured feed per call, never "all"')
        if source_sets is None:
            raise HTTPException(
                status_code=503,
                detail='no source-set catalogue loaded (scaffold-mock mode — set DATABASE_URL)')
        feeds = _feeds()
        source = feeds.get(source_id)
        if source is None:
            # Named before it is probed: nothing leaves the process for a feed we do not poll.
            raise HTTPException(status_code=404,
                                detail=f'unknown source_id {source_id!r}')

        diagnosis = diagnose_feed(source.source_id, source.url,
                                  timeout=_TIMEOUT_SECONDS,
                                  disabled=not source.enabled,
                                  expected_max_age_hours=source.expected_max_age_hours)
        payload = to_jsonable(diagnosis)
        redacted: List[str] = []
        for field in _SCRUBBED:
            value = payload.get(field)
            if not isinstance(value, str) or not value:
                continue
            masked, changed = redact(value)
            if changed:
                payload[field] = masked
                redacted.append(field)
        # Named rather than counted, like the config route: there are four candidate fields, so a
        # reader can see *which* text was altered instead of only how much.
        return FeedDiagnosisResponse(name=name, generated_at=datetime.now(timezone.utc),
                                     source_id=source_id, diagnosis=payload, redacted=redacted)

    def _memory() -> MemoryDiagnosisResponse:
        """The memory census, taken now. Nothing leaves the process and nothing is redacted: the
        payload is counts, type names and source locations of this engine's own code."""
        if memory_census is None:
            raise HTTPException(status_code=503,
                                detail='no memory census installed in this process')
        diagnosis = memory_census.diagnose()
        logger.info('[MEMORY] census served: %d tracked objects, %.0f ms',
                    diagnosis.gc_tracked_objects, diagnosis.census_ms)
        return MemoryDiagnosisResponse(name='memory', generated_at=datetime.now(timezone.utc),
                                       diagnosis=to_jsonable(diagnosis))

    return router
