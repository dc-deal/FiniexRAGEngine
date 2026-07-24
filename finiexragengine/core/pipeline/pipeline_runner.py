"""Pipeline runner — one pipeline pass, top-down: ingest -> per-symbol eval -> assemble (ISSUE_7)."""
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Dict, List, Optional

from finiexragengine.core.observability.cost_recorder import CostRecorder
from finiexragengine.core.observability.source_reach import SourceReach
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.pipeline.envelope_contract import hold_result, taxonomy_type
from finiexragengine.core.pipeline.ingestor import Ingestor
from finiexragengine.core.pipeline.output_guard import OutputGuard
from finiexragengine.core.pipeline.symbol_evaluator import SymbolEvaluator
from finiexragengine.exceptions.ragengine_errors import FiniexRagError
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig, SymbolSpec
from finiexragengine.types.eval_types import SymbolEval
from finiexragengine.types.ingest_types import IngestResult, ReachCensus
from finiexragengine.types.outcome_types import (
    AnalysisEnvelope,
    RunError,
    RunMetadata,
    SentimentResult,
)
from finiexragengine.types.prompt_metadata import PromptMetadata

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Executes one pipeline pass end-to-end and assembles the outcome envelope.

    The top-down flow of ISSUE_7, one readable unit:

        A  ingest      fetch -> embed only new -> upsert (inline in this first slice;
                       moves to the ingest worker with ISSUE_10)
        B+C  per symbol retrieve -> prompt -> LLM -> enriched SentimentResult
        D  assemble    stage timings + tokens/cost into RunMetadata, prompt fingerprint
                       (ISSUE_33) onto the envelope, status derived from what survived

    Envelope invariants (output contract): every requested symbol is always present in
    `result` (a failed symbol degrades to a HOLD row, never a gap); `partial` is preferred
    over `error`; `error` is reserved for a pass where not a single symbol evaluated;
    every RunError carries a fixed taxonomy type. Persisting the envelope is ISSUE_8.
    """

    def __init__(self, config: PipelineConfig, ingestor: Optional[Ingestor],
                 evaluator: SymbolEvaluator, prompt_metadata: PromptMetadata,
                 llm_model: str, cost_recorder: Optional[CostRecorder] = None,
                 outcome_store: Optional[OutcomeStore] = None,
                 source_reach: Optional[SourceReach] = None) -> None:
        self._config = config
        # Output consistency guard (ISSUE_35): deterministic coherence check over each
        # scored row, built from the constellation's tolerances, applied in phase B+C below.
        self._guard = OutputGuard(config.output_guard)
        # None = worker mode (ISSUE_10): acquisition runs on the ingest worker's own
        # clock; this runner only evaluates over the shared corpus. Set = the manual,
        # self-contained pass (run CLI / API without workers) — ingest inline as before.
        self._ingestor = ingestor
        self._evaluator = evaluator
        # Config ∩ health for the referenced source-set (ISSUE_10): both envelope reach numbers
        # come from here, resolved per run. None = no reach available (a caller with no health
        # store) — the envelope then reports 0/0 rather than a made-up full reach.
        self._source_reach = source_reach
        # Resolved once at assembly (ISSUE_33): stamped on every envelope this runner
        # produces, so the outcome names the exact prompt even when every eval fails.
        self._prompt_metadata = prompt_metadata
        self._llm_model = llm_model
        # Optional: the run's own USD is read as a session delta off the shared recorder
        # (single pass at a time), covering embeddings *and* LLM in one number.
        self._cost_recorder = cost_recorder
        # Optional (ISSUE_8): every produced envelope is persisted before it is served —
        # the store is the source of truth; /latest reads it instead of re-running.
        self._outcome_store = outcome_store

    def run(self) -> AnalysisEnvelope:
        run_start = perf_counter()
        usd_before = self._cost_recorder.session_usd if self._cost_recorder else 0.0
        errors: List[RunError] = []

        # --- A: ingest (fetch -> embed only new -> idempotent upsert), per source ---
        # Skipped in worker mode: the ingest worker owns acquisition on its own cadence
        # (ISSUE_10); an empty IngestResult keeps the assembly below uniform.
        ingest = self._ingestor.run() if self._ingestor is not None else IngestResult()
        # Reach is read here, right after acquisition: an inline pass has just recorded its polls
        # into source_health, so the census sees this run's own work; in worker mode it sees the
        # ingest worker's latest. Reading it in phase A (rather than at metadata assembly) is what
        # lets a gap degrade the run — and keeps source errors ahead of eval errors in the list.
        census = (self._source_reach.census() if self._source_reach is not None
                  else ReachCensus(configured=0, reached=0))
        # A source this pass tried and could not fetch — reported with the fetch's own message.
        for source_id, message in ingest.failed_sources.items():
            errors.append(self._error('SOURCE_UNREACHABLE', f'{source_id}: {message}'))
        # A source that is not delivering without *this* pass noticing: in cool-off, never polled —
        # and in worker mode all of them, since acquisition runs on someone else's clock. Without
        # this, the identical state of the world degraded an inline run and passed a worker run as
        # clean. Deduplicated against the fetch failures just reported, which say it better.
        for entry in census.unreached:
            if entry.source_id not in ingest.failed_sources:
                errors.append(self._error('SOURCE_UNREACHABLE',
                                          f'{entry.source_id}: {entry.reason}'))

        # --- B+C: evaluate every requested symbol; a failure degrades, never skips ---
        results: List[SentimentResult] = []
        evals: List[SymbolEval] = []
        per_symbol_tokens: Dict[str, int] = {}
        for spec in self._config.active_symbols():
            try:
                ev = self._evaluator.evaluate(spec.key, spec.retrieval_query())
            except FiniexRagError as exc:
                # Contract: the symbol stays present — degraded to a clean HOLD row,
                # the cause recorded under its taxonomy type.
                error_type = taxonomy_type(exc)
                errors.append(self._error(error_type, f'{spec.key}: {exc}'))
                results.append(self._stamp(hold_result(
                    spec.key, f'Analysis degraded to HOLD ({error_type})'), spec))
                continue
            evals.append(ev)
            per_symbol_tokens[spec.key] = ev.usage.total_tokens
            # Output guard (ISSUE_35): schema-valid but internally contradictory rows (a BUY
            # with a negative score, a near-certain HOLD, an empty reasoning) degrade to the
            # contract HOLD under PARTIAL_RESPONSE — the run turns 'partial'. The SymbolEval
            # above stays in `evals` untouched: the call's tokens/cost/timings are real, and
            # the raw model output remains persisted for inspection (ISSUE_36). A degraded
            # row has urgency 0.0 / is_breaking False — it can never push breaking.
            violations = self._guard.violations(ev.result)
            if violations:
                errors.append(self._error(
                    'PARTIAL_RESPONSE',
                    f"{spec.key}: output guard: {'; '.join(str(v) for v in violations)}"))
                results.append(self._stamp(hold_result(
                    spec.key,
                    f"Output guard degraded to HOLD "
                    f"({', '.join(v.rule for v in violations)})"), spec))
                continue
            results.append(self._stamp(ev.result, spec))

        # --- D: assemble metadata + envelope ---
        stage_timings = list(ingest.stage_timings)
        for ev in evals:
            stage_timings.extend(ev.stage_timings)
        # Served-model trace: normally one snapshot per run; a mid-run alias retarget
        # would show as several (joined) — visible either way.
        snapshots = sorted({ev.model_snapshot for ev in evals if ev.model_snapshot})
        metadata = RunMetadata(
            model=self._llm_model,
            model_snapshot=', '.join(snapshots),
            sources_configured=census.configured,
            sources_reached=census.reached,
            articles_found=ingest.fetched,
            articles_relevant=sum(len(ev.articles) for ev in evals),
            processing_time_ms=(perf_counter() - run_start) * 1000.0,
            stage_timings=stage_timings,
            prompt_tokens=sum(ev.usage.prompt_tokens for ev in evals),
            completion_tokens=sum(ev.usage.completion_tokens for ev in evals),
            cost_usd=(self._cost_recorder.session_usd - usd_before
                      if self._cost_recorder else 0.0),
            per_symbol_tokens=per_symbol_tokens,
            # Retrieval funnel per evaluated symbol (ISSUE_24): a thin or empty context
            # is explainable from the persisted envelope, not just asserted.
            per_symbol_retrieval={ev.result.symbol: ev.retrieval for ev in evals
                                  if ev.retrieval is not None},
            # Fan-out hints (ISSUE_42): set by registry expansion, absent otherwise.
            variant_group=self._config.variant_group,
            variant=self._config.variant,
        )
        envelope = AnalysisEnvelope(
            pipeline_id=self._config.pipeline_id,
            outcome_type=self._config.outcome_type,
            prompt_version=self._prompt_metadata.version,
            prompt_id=self._prompt_metadata.id,
            prompt_hash=self._prompt_metadata.content_hash,
            timestamp=datetime.now(timezone.utc),   # real-time wall clock (live service)
            status=self._derive_status(errors, evals),
            result=results,
            metadata=metadata,
            errors=errors,
        )
        self._persist(envelope, evals)
        return envelope

    def _persist(self, envelope: AnalysisEnvelope, evals: List[SymbolEval]) -> None:
        """Persist the envelope + per-symbol raw LLM output (ISSUE_8/36) — never fatal.

        The raw scored JSON is irreconstructable after the call, so it is stored next to
        the normalized envelope (same row). A store failure must not lose the produced
        envelope for the caller: it degrades the pass (VECTOR_STORE_ERROR, success ->
        partial) and is logged — the envelope is still served.
        """
        if self._outcome_store is None:
            return
        raw_output = {ev.result.symbol: ev.raw_output for ev in evals if ev.raw_output}
        try:
            self._outcome_store.save(envelope, raw_output or None)
        except FiniexRagError as exc:
            envelope.errors.append(self._error('VECTOR_STORE_ERROR',
                                               f'outcome not persisted: {exc}'))
            if envelope.status == 'success':
                envelope.status = 'partial'

    def _derive_status(self, errors: List[RunError],
                       evals: List[SymbolEval]) -> str:
        """success = clean pass · partial = degraded but data · error = nothing evaluated.

        A budget suspend (ISSUE_47) is a *controlled, temporary* degrade — every symbol still
        carries a HOLD row, so `result` is not empty; it is 'partial' (auditable, expected), never
        'error'. 'error' stays reserved for a genuine total failure (nothing evaluated, no budget)."""
        if any(error.type == 'BUDGET_EXCEEDED' for error in errors):
            return 'partial'
        if not evals:
            return 'error'
        return 'partial' if errors else 'success'

    @staticmethod
    def _stamp(result: SentimentResult, spec: SymbolSpec) -> SentimentResult:
        """Attach the instrument's pair legs (ISSUE_70) to a result row — llm or degraded HOLD alike,
        so every emitted symbol carries its `base_currency`/`quote_currency`."""
        result.base_currency = spec.base
        result.quote_currency = spec.quote
        return result

    def _error(self, error_type: str, message: str) -> RunError:
        # Every RunError is logged with its taxonomy type (CLAUDE.md) — but the durable
        # error statistics aggregate from the persisted envelopes' errors, never from logs.
        logger.warning('[%s] %s: %s', error_type, self._config.pipeline_id, message)
        return RunError(type=error_type, message=message,
                        timestamp=datetime.now(timezone.utc))
