"""`GET /v1/stream/{pipeline_id}` — the live signal transport (ISSUE_9 §3).

**Transport only.** What a connect resolves to is `StreamReplay`'s decision, what a frame looks like
is `stream_frames`', and what to deliver next is the dispatcher's. This file owns parameters, the
grant, the socket, and the two things only a connection can own: the heartbeat and the drop.

**The pipeline is a PATH segment, and that is a security property rather than a style choice.**
Authorization derives the grant from the matched route's first path parameter, so the
`?pipeline=<id>` form the published sample first showed would have been *authenticated but ungated* —
reachable by any valid token, including one entitled to nothing — and invisible to the suite's walk
over identity routes, which is the guard built to make exactly that unforgettable. Changed with the
consumer before they built against it (agreed 2026-08-27).

**Nothing is ever filtered out of this stream.** The series is promised gapless and the consumer
treats a `seq` gap as loss immediately, with no grace period, so anything withheld — an error
envelope, a status someone finds noisy — punches a hole indistinguishable from a dropped frame and
fires their recovery for nothing. An `error` envelope is a frame because its `seq` exists.
"""
import logging
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query, Security
from fastapi.responses import StreamingResponse

from finiexragengine.core.outcome.stream_dispatcher import StreamDispatcher
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.core.outcome.stream_session import StreamSession
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.exceptions.ragengine_errors import PipelineNotFoundError
from finiexragengine.types.config_types.app_config_types import StreamConfig

logger = logging.getLogger(__name__)

# Proxy hint: keep an SSE response unbuffered. Harmless where the proxy already knows (Caddy), and
# the difference between "frames arrive" and "frames arrive in one lump at close" where it does not.
_SSE_HEADERS = {'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}


def build_stream_router(dispatcher: StreamDispatcher, replay: StreamReplay,
                        registry: PipelineRegistry, config: StreamConfig,
                        grant: Optional[Callable[..., None]] = None) -> APIRouter:
    """Build the stream router. `scopes=['pipelines']` — a stream *is* the pipeline's series.

    Declared on the router, so this route and any added here later are gated by construction. No new
    grant surface: the thing being addressed is the pipeline, and a grant names a thing rather than a
    transport (`pipelines:crypto_sentiment` governs `/latest`, `/envelopes` and this alike).
    """
    guards = [Security(grant, scopes=['pipelines'])] if grant is not None else []
    router = APIRouter(prefix='/v1/stream', tags=['stream'], dependencies=guards)
    # The frame sequence lives in its own unit: this file is transport, and an SSE sequence is
    # assertable without a socket only if it is not welded to a response object.
    session = StreamSession(dispatcher, replay, config)

    @router.get('/{pipeline_id}')
    async def stream(pipeline_id: str,
                     history: Optional[int] = Query(
                         None, ge=0,
                         description='N frames before live; defaults to 1, and 0 means live only'),
                     since: Optional[int] = Query(
                         None, ge=0, description='replay ascending from since+1; needs `epoch`'),
                     epoch: Optional[int] = Query(
                         None, ge=0, description='the stream_epoch `since` belongs to')
                     ) -> StreamingResponse:
        """Open a stream. Every envelope this pipeline produces becomes a frame, error ones included.

        The parameter rules are refusals, never silent precedences (agreed with the consumer):
        `history` and `since` are mutually exclusive, and `since`/`epoch` are meaningless apart — a
        cursor is `(epoch, seq)`, so serving `since+1..` of a series the caller may not be on is
        worse than rejecting the request. Accepting a parameter and then ignoring it is the defect
        the report surface already refuses to commit.
        """
        try:
            registry.get(pipeline_id)
        except PipelineNotFoundError as exc:
            # 404 rather than an empty stream: "exists but idle" and "does not exist" are different
            # operator situations, and a client that cannot tell them apart waits forever on a typo.
            raise HTTPException(status_code=404, detail=str(exc))
        if not config.enabled:
            raise HTTPException(status_code=503, detail='stream transport is disabled')
        if history is not None and since is not None:
            raise HTTPException(
                status_code=400,
                detail='history and since are mutually exclusive — history is the connect '
                       'snapshot, since is a resync; pick one')
        if (since is None) != (epoch is None):
            raise HTTPException(
                status_code=400,
                detail='since and epoch are meaningless apart: a cursor is (epoch, seq)')

        return StreamingResponse(
            session.frames(pipeline_id, history, since, epoch),
            media_type='text/event-stream', headers=_SSE_HEADERS)

    return router
