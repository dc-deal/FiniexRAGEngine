"""The pipeline-listing endpoint (ISSUE_98 split it off `health_router`)."""
from typing import Callable, Optional

from fastapi import APIRouter, Request, Security

from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.api.token_registry import TokenRegistry
from finiexragengine.types.api_types import PipelineInfo, PipelinesResponse, StreamInfo
from finiexragengine.types.config_types.app_config_types import StreamConfig


def build_pipelines_router(registry: PipelineRegistry,
                           stream: StreamConfig,
                           tokens: Optional[TokenRegistry] = None,
                           grant: Optional[Callable[..., None]] = None) -> APIRouter:
    """`GET /v1/pipelines` — what this engine produces.

    `stream` is **required and not defaulted**, deliberately. Two of its leaves are served to the
    consumer as the numbers their watchdog and their replay window are derived from, and a router
    that could fall back to a schema default would be free to serve numbers the running engine is
    not using — the same "one fact, two copies" defect this file's cadence field just had.

    It lives here rather than beside `/health` because the two sit on opposite sides of the
    authentication boundary (ISSUE_98): `/health` is the one documented public exemption, this is
    not. Leaving it in a file named `health_router` would have made the file's name disagree with
    its contents, and would have made the split easy to undo by accident.
    """
    # The listing has no identity segment, so nothing to gate — it is filtered below. The surface
    # is declared anyway, so a route added to this router later is gated by construction.
    guards = [Security(grant, scopes=['pipelines'])] if grant is not None else []
    router = APIRouter(prefix='/v1', tags=['pipelines'], dependencies=guards)

    def _permitted(request: Request, pipeline_id: str) -> bool:
        """For FILTERING this listing; the gate on `/latest` is `grant_auth` at the router."""
        consumer = getattr(request.state, 'consumer', None)
        return (consumer is None or tokens is None
                or tokens.may(consumer, f'pipelines:{pipeline_id}'))

    @router.get('/pipelines', response_model=PipelinesResponse)
    def list_pipelines(request: Request) -> PipelinesResponse:
        """The pipelines **this caller** may read (ISSUE_104).

        Filtered rather than complete: a consumer holding `pipelines:crypto_sentiment` has no
        business learning that a second stream exists, and a listing that showed it would turn
        every grant into a discovery of a 403.
        """
        infos = [
            PipelineInfo(
                pipeline_id=pipeline.get_config().pipeline_id,
                outcome_type=pipeline.get_config().outcome_type,
                market=pipeline.get_config().market,
                symbols=pipeline.get_config().symbol_keys(),
                trigger_type=pipeline.get_config().trigger.type,
                # `TriggerConfig.cadence_seconds` and nothing else — it is, in its own words, "the
                # one place the two knobs collapse to a number", and `/health` reads it for the
                # same pipeline's eval worker. This file used to convert `trigger.timeframe`
                # itself, so one fact had two derivations that agreed by arithmetic rather than by
                # construction; the consumer asked which of the two served numbers was
                # authoritative, which is the question a second copy always eventually raises.
                cadence_seconds=pipeline.get_config().trigger.cadence_seconds,
            )
            for pipeline in registry.list_pipelines()
            if _permitted(request, pipeline.get_config().pipeline_id)
        ]
        return PipelinesResponse(
            # Engine-wide, so it is built from the configuration this process runs on — never from
            # a default. Serving it at all is what stops the consumer configuring a second answer.
            stream=StreamInfo(heartbeat_seconds=stream.heartbeat_seconds,
                              replay_window_hours=stream.replay_window_hours),
            pipelines=infos)

    return router
