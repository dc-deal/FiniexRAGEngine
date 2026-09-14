"""`GET /v1/pipelines/{pipeline_id}/envelopes` — a bounded range of the series (ISSUE_9 §2).

**Its own address because it is its own question.** A range of the series is not a narrowing of
`/latest`; it is a different thing to ask, and a parameter that turned one into the other would be a
second program wearing the first one's name. `/latest` answers "what is the current signal", this
answers "what did I miss" — and it exists because `/latest` *cannot* answer the second: everything
produced between two polls that is no longer newest at poll time is never fetched, systematically the
out-of-band breaking passes.

It shares `StreamReplay` with the stream, so the two surfaces cannot disagree about whether a cursor
is too old, too new, or from a series that no longer exists. The renderings differ — frames there,
JSON plus a status code here — and the decision does not.
"""
import logging
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query, Security

from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.exceptions.ragengine_errors import FiniexRagError, PipelineNotFoundError
from finiexragengine.types.api_types import EnvelopeRange

logger = logging.getLogger(__name__)


def build_envelopes_router(replay: StreamReplay, registry: PipelineRegistry,
                           grant: Optional[Callable[..., None]] = None,
                           max_limit: int = 500) -> APIRouter:
    """Build the range router. `scopes=['pipelines']` — the same thing `/latest` and the stream name.

    `max_limit` bounds one request. A collector catching up after an outage will happily ask for
    everything, and an unbounded range read of a table that grows for years is the one request that
    can make a diagnostic call look like an incident.
    """
    guards = [Security(grant, scopes=['pipelines'])] if grant is not None else []
    router = APIRouter(prefix='/v1/pipelines', tags=['pipelines'], dependencies=guards)

    @router.get('/{pipeline_id}/envelopes', response_model=EnvelopeRange)
    def envelopes(pipeline_id: str,
                  since: int = Query(..., ge=0, description='return envelopes with seq > since'),
                  epoch: int = Query(..., ge=0, description='the stream_epoch `since` belongs to'),
                  limit: Optional[int] = Query(None, ge=1)) -> EnvelopeRange:
        """Everything after `since`, ascending, bounded.

        `since` and `epoch` are both **required** here, where the stream treats them as an optional
        pair — because there is no other way to call this route. A cursor is `(epoch, seq)`, and a
        range served against an epoch the caller never held would be numbers they believe they have
        seen carrying content they never have.
        """
        try:
            registry.get(pipeline_id)
        except PipelineNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        try:
            plan = replay.plan(pipeline_id, since=since, epoch=epoch,
                               limit=min(limit or max_limit, max_limit))
        except FiniexRagError as exc:
            # The store could not be read: an availability problem, not a bad request, and a caller
            # catching up needs to tell the two apart before it decides whether to retry.
            logger.warning('[ENVELOPES] %s could not be read: %s', pipeline_id, exc)
            raise HTTPException(status_code=503, detail=f'journal unavailable: {exc}')

        if plan.terminal:
            # Terminal on the stream, 409 here — the caller's cursor is unusable either way, and
            # returning rows would be the worst available answer. The `code` keeps the two
            # diagnoses apart: `epoch_changed` means we rewound, `cursor_ahead` means they did.
            raise HTTPException(status_code=409, detail={
                'code': plan.control.code, 'stream_epoch': plan.head.epoch,
                **plan.control.fields})

        truncated = plan.control is not None and plan.control.code == 'replay_truncated'
        return EnvelopeRange(
            pipeline_id=pipeline_id, stream_epoch=plan.head.epoch, head_seq=plan.head.seq,
            envelopes=plan.envelopes, truncated=truncated,
            oldest_available_seq=(plan.control.fields.get('oldest_available_seq')
                                  if truncated else None))

    return router
