"""The pipeline-listing endpoint (ISSUE_98 split it off `health_router`)."""
from typing import Optional

from fastapi import APIRouter

from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.api_types import PipelineInfo, PipelinesResponse
from finiexragengine.utils.timeframe import timeframe_minutes


def _cadence_seconds(timeframe: Optional[str]) -> Optional[int]:
    """The trigger's timeframe as seconds — None when it carries none.

    An unknown token would raise here rather than on the worker's first tick, which is the wrong
    place to find out: the listing must stay answerable even when a pipeline is misconfigured, so
    an unparseable timeframe reports as absent and the configuration error surfaces where it is
    acted on (the supervisor refuses to schedule it).
    """
    if timeframe is None:
        return None
    try:
        return timeframe_minutes(timeframe) * 60
    except ValueError:
        return None


def build_pipelines_router(registry: PipelineRegistry) -> APIRouter:
    """`GET /v1/pipelines` — what this engine produces.

    It lives here rather than beside `/health` because the two sit on opposite sides of the
    authentication boundary (ISSUE_98): `/health` is the one documented public exemption, this is
    not. Leaving it in a file named `health_router` would have made the file's name disagree with
    its contents, and would have made the split easy to undo by accident.
    """
    router = APIRouter(prefix='/v1', tags=['pipelines'])

    @router.get('/pipelines', response_model=PipelinesResponse)
    def list_pipelines() -> PipelinesResponse:
        infos = [
            PipelineInfo(
                pipeline_id=pipeline.get_config().pipeline_id,
                outcome_type=pipeline.get_config().outcome_type,
                market=pipeline.get_config().market,
                symbols=pipeline.get_config().symbol_keys(),
                trigger_type=pipeline.get_config().trigger.type,
                cadence_seconds=_cadence_seconds(pipeline.get_config().trigger.timeframe),
            )
            for pipeline in registry.list_pipelines()
        ]
        return PipelinesResponse(pipelines=infos)

    return router
