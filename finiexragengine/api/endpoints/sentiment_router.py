"""Pipeline run + latest-outcome endpoints."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from finiexragengine.core.pipeline.pipeline import Pipeline
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.pipeline.pipeline_runner import hold_result, taxonomy_type
from finiexragengine.exceptions.ragengine_errors import PipelineNotFoundError
from finiexragengine.types.outcome_types import (
    RunError,
    RunMetadata,
    SentimentEnvelope,
)

logger = logging.getLogger(__name__)


def _error_envelope(pipeline: Pipeline, exc: Exception) -> SentimentEnvelope:
    """The contract's catch-all: a parseable envelope even on internal failure.

    Never a bare 500 — the collector must be able to parse every response. Every
    requested symbol is still present (degraded HOLD rows), the cause lands in
    `errors` under its taxonomy type, `status='error'` marks the pass as unusable.
    """
    config = pipeline.get_config()
    error_type = taxonomy_type(exc)
    return SentimentEnvelope(
        pipeline_id=config.pipeline_id,
        outcome_type=config.outcome_type,
        prompt_version=config.prompt.version,
        timestamp=datetime.now(timezone.utc),
        status='error',
        result=[hold_result(symbol, f'Run failed ({error_type})')
                for symbol in config.symbols],
        metadata=RunMetadata(model='unavailable',
                             sources_configured=len(config.sources)),
        errors=[RunError(type=error_type, message=str(exc),
                         timestamp=datetime.now(timezone.utc))],
    )


def build_sentiment_router(registry: PipelineRegistry) -> APIRouter:
    """Build the pipeline run/latest router bound to the given registry."""
    router = APIRouter(prefix='/v1/pipelines', tags=['pipelines'])

    @router.post('/{pipeline_id}/run', response_model=SentimentEnvelope)
    def run_pipeline(pipeline_id: str) -> SentimentEnvelope:
        """Force a fresh run and return its outcome envelope (ISSUE_7 staged flow)."""
        try:
            pipeline = registry.get(pipeline_id)
        except PipelineNotFoundError as exc:
            # An unknown pipeline is a caller error, not a run failure — plain 404.
            raise HTTPException(status_code=404, detail=str(exc))
        try:
            return pipeline.run()
        except Exception as exc:   # noqa: BLE001 — the contract demands a parseable envelope
            logger.exception('pipeline %s run failed', pipeline_id)
            return _error_envelope(pipeline, exc)

    @router.get('/{pipeline_id}/latest', response_model=SentimentEnvelope)
    def latest(pipeline_id: str) -> SentimentEnvelope:
        """Return the last cached outcome instantly (the live-bot path).

        TODO(impl): read from OutcomeStore.get_latest(pipeline_id) (ISSUE_8). Until
        persistence exists this triggers a fresh run — same envelope contract.
        """
        try:
            pipeline = registry.get(pipeline_id)
        except PipelineNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        try:
            return pipeline.run()
        except Exception as exc:   # noqa: BLE001
            logger.exception('pipeline %s latest failed', pipeline_id)
            return _error_envelope(pipeline, exc)

    return router
