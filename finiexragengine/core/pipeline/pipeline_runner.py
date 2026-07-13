"""Pipeline runner — one pipeline pass, top-down: ingest -> per-symbol eval -> assemble (ISSUE_7)."""
import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Dict, List, Optional

from finiexragengine.core.observability.run_footer import RunFooter
from finiexragengine.core.pipeline.ingestor import Ingestor, IngestResult
from finiexragengine.core.pipeline.symbol_evaluator import SymbolEval, SymbolEvaluator
from finiexragengine.exceptions.ragengine_errors import (
    FiniexRagError,
    LLMApiError,
    LLMParseError,
    LLMTimeoutError,
    SourceFetchError,
    VectorStoreError,
)
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.outcome_types import (
    AnalysisEnvelope,
    RunError,
    RunMetadata,
    SentimentResult,
)
from finiexragengine.types.prompt_metadata import PromptMetadata

logger = logging.getLogger(__name__)

# The fixed RunError taxonomy (output contract): exact exception class -> type string.
# Anything unmapped degrades under PARTIAL_RESPONSE — the row-was-degraded marker.
_ERROR_TAXONOMY = {
    SourceFetchError: 'SOURCE_UNREACHABLE',
    LLMTimeoutError: 'LLM_TIMEOUT',
    LLMApiError: 'LLM_API_ERROR',
    LLMParseError: 'LLM_PARSE_ERROR',
    VectorStoreError: 'VECTOR_STORE_ERROR',
}


def taxonomy_type(exc: Exception) -> str:
    """Map an engine exception to its fixed RunError.type (fallback: PARTIAL_RESPONSE)."""
    for exc_class, type_string in _ERROR_TAXONOMY.items():
        if isinstance(exc, exc_class):
            return type_string
    return 'PARTIAL_RESPONSE'


def hold_result(symbol: str, reasoning: str,
                basis: str = 'degraded') -> SentimentResult:
    """A contract HOLD row: HOLD / 0.0 / reason / no sources — never a missing symbol.

    `basis` tags how the row came to be (ISSUE_24/35): the runner/API use 'degraded'
    (a failure forced the HOLD); the evaluator's data-shortage shortcut emits its own
    row with 'no_data'.
    """
    return SentimentResult(symbol=symbol, signal='HOLD', sentiment_score=0.0,
                           confidence=0.0, reasoning=reasoning, basis=basis)


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
                 llm_model: str, cost_recorder=None, outcome_store=None,
                 sources_configured: int = 0) -> None:
        self._config = config
        # None = worker mode (ISSUE_10): acquisition runs on the ingest worker's own
        # clock; this runner only evaluates over the shared corpus. Set = the manual,
        # self-contained pass (run CLI / API without workers) — ingest inline as before.
        self._ingestor = ingestor
        self._evaluator = evaluator
        # The referenced source-set's size — config no longer owns feeds (ISSUE_10).
        self._sources_configured = sources_configured
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
        for source_id, message in ingest.failed_sources.items():
            errors.append(self._error('SOURCE_UNREACHABLE', f'{source_id}: {message}'))

        # --- B+C: evaluate every requested symbol; a failure degrades, never skips ---
        results: List[SentimentResult] = []
        evals: List[SymbolEval] = []
        per_symbol_tokens: Dict[str, int] = {}
        for symbol in self._config.symbols:
            query = self._config.symbol_queries.get(symbol, symbol)
            try:
                ev = self._evaluator.evaluate(symbol, query)
            except FiniexRagError as exc:
                # Contract: the symbol stays present — degraded to a clean HOLD row,
                # the cause recorded under its taxonomy type.
                error_type = taxonomy_type(exc)
                errors.append(self._error(error_type, f'{symbol}: {exc}'))
                results.append(hold_result(
                    symbol, f'Analysis degraded to HOLD ({error_type})'))
                continue
            evals.append(ev)
            results.append(ev.result)
            per_symbol_tokens[symbol] = ev.usage.total_tokens

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
            sources_configured=self._sources_configured,
            sources_reached=self._sources_configured - len(ingest.failed_sources),
            articles_found=ingest.fetched,
            articles_relevant=sum(len(ev.articles) for ev in evals),
            processing_time_ms=(perf_counter() - run_start) * 1000.0,
            stage_timings=stage_timings,
            prompt_tokens=sum(ev.usage.prompt_tokens for ev in evals),
            completion_tokens=sum(ev.usage.completion_tokens for ev in evals),
            cost_usd=(self._cost_recorder.session_usd - usd_before
                      if self._cost_recorder else 0.0),
            per_symbol_tokens=per_symbol_tokens,
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
        """success = clean pass · partial = degraded but data · error = nothing evaluated."""
        if not evals:
            return 'error'
        return 'partial' if errors else 'success'

    def _error(self, error_type: str, message: str) -> RunError:
        # Every RunError is logged with its taxonomy type (CLAUDE.md) — but the durable
        # error statistics aggregate from the persisted envelopes' errors, never from logs.
        logger.warning('[%s] %s: %s', error_type, self._config.pipeline_id, message)
        return RunError(type=error_type, message=message,
                        timestamp=datetime.now(timezone.utc))


def format_envelope_run(envelope: AnalysisEnvelope) -> str:
    """Render a full run envelope as the console pattern: header, signal table, metrics.

    The run CLI's output — ends with the shared `--- run metrics ---` footer (every
    spending pass reports its own cost, per CLAUDE.md).
    """
    m = envelope.metadata
    fingerprint = (f'prompt {envelope.prompt_id}@v{envelope.prompt_version} '
                   f'#{envelope.prompt_hash}' if envelope.prompt_id else 'prompt (mock)')
    lines = [
        f'=== Run: {envelope.pipeline_id}   ({envelope.outcome_type} · {fingerprint}) ===',
        f'  status      {envelope.status}     sources {m.sources_reached}/{m.sources_configured}'
        f'   articles {m.articles_found} found · {m.articles_relevant} relevant',
        '',
        f'  {"symbol":10} {"signal":6} {"score":>6} {"conf":>5} {"urg":>5}  brk  sources  basis',
    ]
    for r in envelope.result:
        lines.append(f'  {r.symbol:10} {r.signal:6} {r.sentiment_score:>+6.2f} '
                     f'{r.confidence:>5.2f} {r.urgency:>5.2f}  {"yes" if r.is_breaking else "no ":3} '
                     f'{len(r.sources):>7}  {r.basis}')
    if envelope.errors:
        lines.append('')
        for error in envelope.errors:
            lines.append(f'  ERROR       [{error.type}] {error.message}')
    if m.model_snapshot == m.model and m.model_snapshot:
        model_label = f'{m.model} (pinned)'
    elif m.model_snapshot:
        model_label = f'{m.model} (served {m.model_snapshot})'
    else:
        model_label = m.model
    footer = RunFooter(
        timings=m.stage_timings,
        tokens_label=f'prompt {m.prompt_tokens} · completion {m.completion_tokens} '
                     f'· total {m.prompt_tokens + m.completion_tokens}',
        usd=m.cost_usd, section='this run', model_label=model_label, aggregate=True)
    lines += ['', footer.render()]
    return '\n'.join(lines)
