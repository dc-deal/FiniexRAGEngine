"""Pipeline run + latest-outcome endpoints."""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.pipeline.envelope_contract import hold_result, taxonomy_type
from finiexragengine.core.pipeline.pipeline import Pipeline
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
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
                for symbol in config.symbol_keys()],
        # sources_configured stays 0 here: the catch-all layer has no resolved
        # source-set (ISSUE_10) and the crash case knows nothing about feed health.
        metadata=RunMetadata(model='unavailable'),
        errors=[RunError(type=error_type, message=str(exc),
                         timestamp=datetime.now(timezone.utc))],
    )


def _store_silent_envelope(pipeline: Pipeline, detail: str) -> SentimentEnvelope:
    """The answer when the store cannot supply an envelope and running would cost money.

    Same shape as any failure: every requested symbol present as a degraded HOLD, `status='error'`,
    the cause in `errors`. `VECTOR_STORE_ERROR` covers both readings — the store failed, or the
    store is empty — and the message carries which; the taxonomy stays closed, as the contract
    requires, and a consumer filters on the type it already knows.
    """
    config = pipeline.get_config()
    return SentimentEnvelope(
        pipeline_id=config.pipeline_id,
        outcome_type=config.outcome_type,
        prompt_version=config.prompt.version,
        timestamp=datetime.now(timezone.utc),
        status='error',
        result=[hold_result(symbol, 'No stored outcome available')
                for symbol in config.symbol_keys()],
        metadata=RunMetadata(model='unavailable'),
        errors=[RunError(type='VECTOR_STORE_ERROR', message=detail,
                         timestamp=datetime.now(timezone.utc))],
    )


def build_sentiment_router(registry: PipelineRegistry,
                           outcome_store: Optional[OutcomeStore] = None,
                           run_enabled: bool = False) -> APIRouter:
    """Build the pipeline run/latest router bound to the given registry.

    `outcome_store` (ISSUE_8) backs `/latest` with persisted envelopes; without one
    (scaffold-mock mode, no DB) `/latest` falls back to a fresh run — free there, because
    scaffold mock makes no LLM call.

    `run_enabled` (ISSUE_98) decides whether `POST /{pipeline_id}/run` exists **at all**.
    Defaults to False: the route converts an HTTP request into OpenAI spend, so it has to be
    switched on deliberately rather than inherited by default.
    """
    router = APIRouter(prefix='/v1/pipelines', tags=['pipelines'])

    def _persist_error(envelope: SentimentEnvelope) -> None:
        # Best effort: error statistics aggregate from *persisted* envelopes, so even
        # the catch-all envelope lands in the store — but persisting a failure report
        # must never be able to fail the response itself.
        if outcome_store is None:
            return
        try:
            outcome_store.save(envelope)
        except Exception:   # noqa: BLE001
            logger.exception('error envelope for %s not persisted', envelope.pipeline_id)

    # ISSUE_98: the route is REGISTERED conditionally, never defined-then-refused. An endpoint
    # that exists and answers 403 is still in the OpenAPI schema, still discoverable, and one
    # config edit from live. One that was never registered cannot be reached at all.
    #
    # Off in production by decision: an external consumer must not be able to cause spend at
    # all. The engine's own workers produce the series, so every paid call originates inside
    # the engine, where the cost log accounts for it. This route was the one hole in that.
    if run_enabled:
        @router.post('/{pipeline_id}/run', response_model=SentimentEnvelope)
        def run_pipeline(pipeline_id: str) -> SentimentEnvelope:
            """Force a fresh run and return its outcome envelope (ISSUE_7 staged flow).

            The runner persists its own envelope (ISSUE_8) — only the catch-all error
            envelope is persisted here.
            """
            try:
                pipeline = registry.get(pipeline_id)
            except PipelineNotFoundError as exc:
                # An unknown pipeline is a caller error, not a run failure — plain 404.
                raise HTTPException(status_code=404, detail=str(exc))
            try:
                # A caller outside the engine asked for this pass (ISSUE_87) — not the engine's own
                # clock. The envelope says so, so a consumer can tell it from the bar-close series.
                return pipeline.run('external')
            except Exception as exc:   # noqa: BLE001 — the contract demands a parseable envelope
                logger.exception('pipeline %s run failed', pipeline_id)
                envelope = _error_envelope(pipeline, exc)
                _persist_error(envelope)
                return envelope

    @router.get('/{pipeline_id}/latest', response_model=SentimentEnvelope)
    def latest(pipeline_id: str) -> SentimentEnvelope:
        """Return the last persisted outcome instantly (the live-bot path, ISSUE_8).

        **This GET never spends.** It reads the store; when the store cannot supply an envelope —
        a read failure, or nothing persisted yet — it returns the contract's error envelope rather
        than running the pipeline. A caller who wants a fresh pass has `POST /run` — where it is
        registered at all (ISSUE_98: off in production, because an external request must not be
        able to cause spend).

        The previous fallback ran a paid pass on both of those paths, which is the wrong trade for
        the caller this endpoint exists for. A polling consumer cannot tell a served envelope from a
        freshly-run one, so a bad minute in the database became continuous spend that looked like
        normal operation. The write path had the same shape and was worse, because it does not clear
        on its own: `_persist` swallows a failed save, so the store stays empty, every poll is
        another cold miss, and every cold miss was another paid run.

        Without a store (scaffold-mock mode) a run is free — no LLM is involved — so that path is
        unchanged.
        """
        try:
            pipeline = registry.get(pipeline_id)
        except PipelineNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if outcome_store is not None:
            try:
                stored = outcome_store.get_latest(pipeline_id)
            except Exception as exc:   # noqa: BLE001 — a parseable envelope, never a 500
                logger.exception('outcome store read failed for %s', pipeline_id)
                return _store_silent_envelope(
                    pipeline, f'outcome store unavailable: {exc}')
            if stored is not None:
                return stored
            # Cold miss: the store works and holds nothing yet. Not persisted — writing this would
            # make it the "latest" and every later call would serve the miss instead of a signal.
            logger.info('no outcome stored yet for %s — returning the empty-store envelope',
                        pipeline_id)
            return _store_silent_envelope(
                pipeline, 'no outcome persisted yet for this pipeline')
        # No store configured. Running here is free **only** in scaffold-mock mode, where no LLM
        # is involved — so ask the pipeline instead of assuming (ISSUE_98). Until now this held
        # because `create_app` never builds a real runner without a store; that is a coincidence
        # of one call site, not a property of this router, and a router assembled the other way
        # would have turned every GET into a paid pass. Same failure shape the store-failure path
        # above already had removed.
        if pipeline.is_attached():
            return _store_silent_envelope(
                pipeline, 'no outcome store configured — refusing to run a paid pass on a GET')
        try:
            # A caller outside the engine asked for this pass (ISSUE_87) — not the engine's own
            # clock. The envelope says so, so a consumer can tell it from the bar-close series.
            return pipeline.run('external')
        except Exception as exc:   # noqa: BLE001
            logger.exception('pipeline %s latest failed', pipeline_id)
            envelope = _error_envelope(pipeline, exc)
            _persist_error(envelope)
            return envelope

    return router
