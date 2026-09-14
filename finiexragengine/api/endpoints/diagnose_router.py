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
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Security
from finiex_auth.grant_auth import build_grant_dependency
from finiex_auth.redaction import redact
from finiex_auth.token_registry import TokenRegistry

from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.sources.feed_doctor import diagnose_feed
from finiexragengine.types.api_types import FeedDiagnosisResponse
from finiexragengine.types.config_types.source_set_types import SourceConfig
from finiexragengine.utils.dataclass_json import to_jsonable

logger = logging.getLogger(__name__)

# The one probe this engine exposes. A closed set rather than a free name, so the grant model
# applies unchanged and a caller cannot reach anything that was not deliberately named.
_PROBES = ('feed',)

# Fields of `FeedDiagnosis` carrying text this engine did not write. `head` is the whole point of
# the route — the first bytes of the remote body — and therefore arbitrary remote content; the
# other three quote the URL, which can carry a feed's key in its query string.
_SCRUBBED: Tuple[str, ...] = ('head', 'url', 'bozo_exception', 'transport_error')

# Half the unit's own default. See the module docstring: this runs in the pool that serves every
# other sync endpoint, so two sequential probes have to stay a bounded wait.
_TIMEOUT_SECONDS = 10


def build_diagnose_router(source_sets: Optional[SourceSetRegistry],
                          tokens: TokenRegistry) -> APIRouter:
    """Probe one configured feed and return what the diagnosis found.

    `source_sets` is the registry this process loaded — the same one the ingest workers poll from,
    so a diagnosis is about a feed the engine actually has. `None` is scaffold-mock mode (no
    database, so no catalogue was built) and the route says so rather than answering for a feed
    list it never loaded.
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

    @router.get('/{name}', response_model=FeedDiagnosisResponse)
    def diagnose(name: str,
                 source_id: str = Query(
                     ..., description='the configured feed to probe — required, never "all"')
                 ) -> FeedDiagnosisResponse:
        """One feed's raw fetch and parse. 404 for an unknown probe or feed, 503 without a catalogue.

        `source_id` has no default on purpose. An omitted parameter answering "every feed" is the
        difference between a diagnostic and an amplifier, and a default is exactly how that arrives
        by accident later.
        """
        if name not in _PROBES:
            raise HTTPException(status_code=404, detail=f'no probe named {name!r}')
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

    return router
