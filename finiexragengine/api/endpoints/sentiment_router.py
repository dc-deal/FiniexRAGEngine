"""Pipeline run + latest-outcome endpoints."""
from fastapi import APIRouter, HTTPException

from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.exceptions.ragengine_errors import PipelineNotFoundError
from finiexragengine.types.outcome_types import SentimentEnvelope


def build_sentiment_router(registry: PipelineRegistry) -> APIRouter:
    """Build the pipeline run/latest router bound to the given registry."""
    router = APIRouter(prefix='/v1/pipelines', tags=['pipelines'])

    @router.post('/{pipeline_id}/run', response_model=SentimentEnvelope)
    def run_pipeline(pipeline_id: str) -> SentimentEnvelope:
        """Force a fresh run and return its outcome envelope.

        Scaffold: returns a deterministic mock envelope (see Pipeline.run).
        """
        try:
            pipeline = registry.get(pipeline_id)
        except PipelineNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return pipeline.run()

    @router.get('/{pipeline_id}/latest', response_model=SentimentEnvelope)
    def latest(pipeline_id: str) -> SentimentEnvelope:
        """Return the last cached outcome instantly (the live-bot path).

        TODO(impl): read from OutcomeStore.get_latest(pipeline_id). The scaffold
        re-runs the mock so the contract is exercisable before persistence exists.
        """
        try:
            pipeline = registry.get(pipeline_id)
        except PipelineNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return pipeline.run()

    return router
