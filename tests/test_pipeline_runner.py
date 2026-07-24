"""Tests for the PipelineRunner (ISSUE_7) — envelope invariants, no DB/API.

Fakes sit at the runner's injection seam (Ingestor / SymbolEvaluator), so these tests
exercise orchestration + assembly only: every-symbol-present, partial-over-error, the
RunError taxonomy, metric capture and the prompt fingerprint (ISSUE_33).
"""
from datetime import datetime, timezone
from typing import List

import pytest

from finiexragengine.core.observability.reports.envelope_report import format_envelope_run
from finiexragengine.core.pipeline.envelope_contract import taxonomy_type
from finiexragengine.core.pipeline.pipeline_runner import PipelineRunner
from finiexragengine.exceptions.ragengine_errors import (
    BudgetExceededError,
    LLMApiError,
    LLMTimeoutError,
    VectorStoreError,
)
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.pipeline_config_types import (
    OutputGuardConfig,
    PipelineConfig,
)
from finiexragengine.types.eval_types import SymbolEval
from finiexragengine.types.ingest_types import (
    IngestResult,
    ReachCensus,
    SourcePoll,
    UnreachedSource,
)
from finiexragengine.types.llm_types import LlmUsage
from finiexragengine.types.outcome_types import (
    ArticleRef,
    RetrievalFunnel,
    SentimentResult,
    StageTiming,
)
from finiexragengine.types.prompt_metadata import PromptMetadata

_TS = datetime(2026, 7, 1, tzinfo=timezone.utc)
_META = PromptMetadata(id='sentiment-crypto', version='1', author='t', created='',
                       description='', content_hash='cafe12345678')


def _config(symbols: List[str]) -> PipelineConfig:
    return PipelineConfig(
        pipeline_id='p', outcome_type='sentiment_fear_greed', market='crypto',
        symbols=[{'key': s, 'base': s[:-3], 'quote': s[-3:]} for s in symbols],
        llm={'model': 'gpt-4o-mini'}, source_set='test_news')


def _article(article_id: str) -> Article:
    return Article(article_id=article_id, source_id='s1', source_weight=1.0,
                   url=f'https://example.test/{article_id}', title='t', summary='s',
                   language='en', published_at=_TS, fetched_at=_TS)


def _timing(stage: str, ms: float) -> StageTiming:
    return StageTiming(stage=stage, started_at=_TS, ended_at=_TS, duration_ms=ms)


def _eval(symbol: str, tokens=(100, 20)) -> SymbolEval:
    # Mirror the evaluator's enrich: the LLM path always attaches provenance from the
    # retrieved articles — the guard's structural backstop relies on it (ISSUE_35).
    article = _article('a')
    result = SentimentResult(symbol=symbol, signal='BUY', sentiment_score=0.5,
                             confidence=0.8, reasoning='bullish',
                             sources=[ArticleRef(article_id=article.article_id,
                                                 url=article.url, title=article.title,
                                                 published_at=article.published_at,
                                                 fetched_at=article.fetched_at)])
    return SymbolEval(result=result, prompt='P', prompt_metadata=_META,
                      usage=LlmUsage(*tokens), articles=[_article('a')],
                      stage_timings=[_timing('retrieve', 10.0), _timing('llm', 90.0)],
                      raw_output={'signal': 'BUY'},
                      model_snapshot='gpt-4o-mini-2024-07-18')


class _FakeIngestor:
    def __init__(self, failed=None):
        self._failed = failed or {}

    def run(self) -> IngestResult:
        # Build the shape the real ingestor produces: a failure is that source's poll record,
        # and `failed_sources` is a view over it — the fake must not invent a flatter truth.
        polls = [SourcePoll(source_id, 'failed', detail=message)
                 for source_id, message in self._failed.items()]
        return IngestResult(fetched=10, embedded=4, stored=4, polls=polls,
                            stage_timings=[_timing('fetch', 100.0), _timing('embed', 50.0)])


class _FakeEvaluator:
    """Evaluates from a symbol -> SymbolEval | Exception map."""
    def __init__(self, outcomes):
        self._outcomes = outcomes

    def evaluate(self, symbol, query):
        outcome = self._outcomes[symbol]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeRecorder:
    """Session accumulator stand-in: pretends the run recorded 0.001 USD."""
    def __init__(self):
        self.session_usd = 0.0

    def tick(self):
        self.session_usd += 0.001


class _FakeStore:
    """Captures save() calls; optionally fails like an unreachable Postgres."""
    def __init__(self, exc=None):
        self.saved = []
        self._exc = exc

    def save(self, envelope, raw_output=None):
        if self._exc is not None:
            raise self._exc
        self.saved.append((envelope, raw_output))


class _FakeReach:
    """Stands in for SourceReach — a fixed census, no DB.

    The runner's job is to *report* the census; deciding what counts as reached (quarantine,
    a failed last poll, `enabled`) belongs to SourceReach and is tested there.
    """

    def __init__(self, configured=2, reached=2, unreached=()):
        self._census = ReachCensus(
            configured=configured, reached=reached,
            unreached=[u if isinstance(u, UnreachedSource) else UnreachedSource(u, 'not delivering')
                       for u in unreached])

    def census(self) -> ReachCensus:
        return self._census


def _runner(config, ingestor, evaluator, recorder=None, store=None, reach=None):
    # The reach census mirrors what the assembler injects for the referenced source-set
    # (ISSUE_10) — the fake set has two feeds, both delivering unless a test says otherwise.
    return PipelineRunner(config, ingestor, evaluator, _META,
                          llm_model='gpt-4o-mini', cost_recorder=recorder,
                          outcome_store=store, source_reach=reach or _FakeReach())


def test_clean_pass_assembles_success_envelope():
    config = _config(['BTCUSD', 'ETHUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD'),
                                       'ETHUSD': _eval('ETHUSD')})).run()
    assert envelope.status == 'success'
    assert [r.symbol for r in envelope.result] == ['BTCUSD', 'ETHUSD']
    # Prompt fingerprint stamped from the resolved metadata (ISSUE_33).
    assert (envelope.prompt_id, envelope.prompt_version, envelope.prompt_hash) == (
        'sentiment-crypto', '1', 'cafe12345678')
    # Metric capture (ISSUE_12): summed tokens, per-symbol footprint, all stage timings.
    assert envelope.metadata.prompt_tokens == 200
    assert envelope.metadata.completion_tokens == 40
    assert envelope.metadata.per_symbol_tokens == {'BTCUSD': 120, 'ETHUSD': 120}
    assert envelope.metadata.articles_found == 10
    assert envelope.metadata.articles_relevant == 2
    assert envelope.metadata.sources_reached == 2
    # Served-model trace next to the configured alias (the model-side prompt_hash).
    assert envelope.metadata.model == 'gpt-4o-mini'
    assert envelope.metadata.model_snapshot == 'gpt-4o-mini-2024-07-18'
    stages = [t.stage for t in envelope.metadata.stage_timings]
    assert stages == ['fetch', 'embed', 'retrieve', 'llm', 'retrieve', 'llm']
    assert envelope.metadata.processing_time_ms > 0.0


def test_result_rows_carry_base_and_quote_currency():
    # ISSUE_70: every emitted row is stamped with its pair legs from the SymbolSpec — an llm row
    # and a degraded HOLD row alike, so a consumer reads base/quote without its own lookup.
    config = _config(['BTCUSD', 'ETHEUR'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD'),
                                       'ETHEUR': LLMTimeoutError('slow')})).run()   # llm + degraded HOLD
    rows = {r.symbol: r for r in envelope.result}
    assert (rows['BTCUSD'].base_currency, rows['BTCUSD'].quote_currency) == ('BTC', 'USD')
    assert (rows['ETHEUR'].base_currency, rows['ETHEUR'].quote_currency) == ('ETH', 'EUR')  # HOLD stamped too


def test_failed_symbol_degrades_to_hold_and_partial():
    config = _config(['BTCUSD', 'ETHUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD'),
                                       'ETHUSD': LLMTimeoutError('too slow')})).run()
    assert envelope.status == 'partial'
    # Contract: the failed symbol is still present — degraded, never missing.
    eth = {r.symbol: r for r in envelope.result}['ETHUSD']
    assert eth.signal == 'HOLD' and eth.confidence == 0.0 and eth.sources == []
    assert 'LLM_TIMEOUT' in eth.reasoning
    assert eth.basis == 'degraded'                     # failure row, not data shortage (ISSUE_24)
    assert [e.type for e in envelope.errors] == ['LLM_TIMEOUT']


def test_incoherent_row_degrades_via_guard_to_hold_and_partial():
    # ISSUE_35: schema-valid but contradictory — a BUY scored negative. The guard degrades
    # the row in place: the symbol stays present, the run turns partial, the cause is a
    # PARTIAL_RESPONSE error naming the rule and the offending value.
    config = _config(['BTCUSD', 'ETHUSD'])
    bad = _eval('ETHUSD')
    bad.result = bad.result.model_copy(update={'sentiment_score': -0.7})
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD'), 'ETHUSD': bad})).run()
    assert envelope.status == 'partial'
    eth = {r.symbol: r for r in envelope.result}['ETHUSD']
    assert eth.signal == 'HOLD' and eth.confidence == 0.0 and eth.basis == 'degraded'
    assert 'signal_score_coherence' in eth.reasoning
    assert not eth.is_breaking                        # a degraded row can never push breaking
    assert [e.type for e in envelope.errors] == ['PARTIAL_RESPONSE']
    assert 'ETHUSD: output guard' in envelope.errors[0].message
    assert '-0.7' in envelope.errors[0].message       # offending value preserved for debugging


def test_guard_degraded_row_keeps_tokens_and_raw_output():
    # The paid call happened: its tokens/cost count, and the raw model output stays
    # persisted next to the envelope (ISSUE_36) — only the served row is swapped.
    store = _FakeStore()
    bad = _eval('BTCUSD')
    bad.result = bad.result.model_copy(update={'sentiment_score': -0.9})
    envelope = _runner(_config(['BTCUSD']), _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': bad}), store=store).run()
    assert envelope.metadata.per_symbol_tokens == {'BTCUSD': 120}
    assert store.saved[0][1] == {'BTCUSD': {'signal': 'BUY'}}
    assert envelope.result[0].signal == 'HOLD'


def test_guard_tolerances_come_from_the_constellation():
    # The knobs live in the constellation (no magic numbers): a widened dead zone lets the
    # same row pass that the default tolerance degrades.
    config = _config(['BTCUSD']).model_copy(
        update={'output_guard': OutputGuardConfig(score_signal_tolerance=0.8)})
    bad = _eval('BTCUSD')
    bad.result = bad.result.model_copy(update={'sentiment_score': -0.7})
    envelope = _runner(config, _FakeIngestor(), _FakeEvaluator({'BTCUSD': bad})).run()
    assert envelope.status == 'success'
    assert envelope.result[0].signal == 'BUY'


def test_budget_exceeded_degrades_to_hold_and_partial():
    # ISSUE_47: a provider quota stop surfaces as BudgetExceededError from the eval seam; the
    # runner degrades the symbol to a clean HOLD tagged BUDGET_EXCEEDED — the contract holds.
    config = _config(['BTCUSD', 'ETHUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD'),
                                       'ETHUSD': BudgetExceededError('provider quota reached')})).run()
    assert envelope.status == 'partial'
    eth = {r.symbol: r for r in envelope.result}['ETHUSD']
    assert eth.signal == 'HOLD' and eth.basis == 'degraded'
    assert [e.type for e in envelope.errors] == ['BUDGET_EXCEEDED']


def test_all_symbols_budget_suspended_is_partial_not_error():
    # A full budget suspend is a controlled degrade — every symbol has a HOLD row, so it is
    # 'partial' (auditable), NOT 'error' (ISSUE_47); 'error' stays for a genuine total failure.
    config = _config(['BTCUSD', 'ETHUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': BudgetExceededError('quota'),
                                       'ETHUSD': BudgetExceededError('quota')})).run()
    assert envelope.status == 'partial'                # not 'error' — a deliberate pause, rows present
    assert {r.symbol for r in envelope.result} == {'BTCUSD', 'ETHUSD'}
    assert all(r.signal == 'HOLD' for r in envelope.result)


def test_failed_source_records_taxonomy_and_partial():
    # The failure itself is what degrades the pass — an error carries the status, not the count.
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(failed={'s2': 'connection refused'}),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}),
                       reach=_FakeReach(configured=2, reached=1, unreached=['s2'])).run()
    assert envelope.status == 'partial'
    assert envelope.errors[0].type == 'SOURCE_UNREACHABLE'
    assert 's2' in envelope.errors[0].message


def test_envelope_reports_the_census_it_is_given_never_a_derivation():
    # The bug this replaces: `reached = configured - len(failed_sources)`. Deriving one number
    # from the other meant they could only ever differ by a *failed fetch* — a source missed for
    # any other reason (quarantine, an aborted pass, an eval that never fetched at all) counted
    # as reached. The runner now reports what the census measured, and nothing else.
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(),          # no failure this pass ...
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}),
                       reach=_FakeReach(configured=7, reached=6, unreached=['boe_news'])).run()

    assert envelope.metadata.sources_configured == 7
    assert envelope.metadata.sources_reached == 6        # ... yet the gap is still reported


def test_a_gap_in_reach_degrades_the_run_even_with_no_failed_fetch():
    # A source missing without *this* pass failing at it — quarantine, or worker mode where the
    # runner never fetches. The count alone used to carry that: reach said 6/7 while the status
    # said `success`, so a consumer reading the status saw a clean run over incomplete data.
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}),
                       reach=_FakeReach(configured=7, reached=6, unreached=[
                           UnreachedSource('boe_news', 'quarantined until 07-18 14:39 UTC')])).run()

    assert envelope.status == 'partial'
    assert envelope.errors[0].type == 'SOURCE_UNREACHABLE'
    assert 'boe_news' in envelope.errors[0].message
    assert 'quarantined until' in envelope.errors[0].message   # the cause is preserved, not just the gap


def test_a_failed_fetch_is_reported_once_not_twice():
    # The source failed the fetch *and* is unreached in the census (the ingestor wrote that very
    # failure into source_health moments earlier). Only the fetch's own message survives — it says
    # more than the census could — and the run must not carry the same source twice.
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(failed={'s2': 'connection refused'}),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}),
                       reach=_FakeReach(configured=2, reached=1, unreached=[
                           UnreachedSource('s2', 'last poll failed (UNREACHABLE, 1 consecutive)')])).run()

    source_errors = [e for e in envelope.errors if e.type == 'SOURCE_UNREACHABLE']
    assert len(source_errors) == 1
    assert 'connection refused' in source_errors[0].message   # the richer of the two texts won


@pytest.mark.parametrize('inline', [True, False], ids=['inline_mode', 'worker_mode'])
def test_the_same_gap_degrades_both_modes(inline):
    # The promise of this design: the mode is a deployment detail, not a fact about the world.
    # An identical health state must yield an identical status whether acquisition ran on this
    # runner's own clock or on the ingest worker's. It did not before — worker mode had no fetch
    # loop, so it had no source errors, so the same missing feed passed as a clean run.
    config = _config(['BTCUSD'])
    envelope = PipelineRunner(config, _FakeIngestor() if inline else None,
                              _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}), _META,
                              llm_model='gpt-4o-mini',
                              source_reach=_FakeReach(configured=7, reached=6,
                                                      unreached=['boe_news'])).run()
    assert envelope.status == 'partial'
    assert envelope.metadata.sources_reached == 6
    assert envelope.errors[0].type == 'SOURCE_UNREACHABLE'


def test_a_disabled_source_never_degrades_the_run():
    # A switched-off feed is not in `configured`, so it is never in `unreached` either: it cannot
    # produce an error. Switching a feed off is a decision — the run is not degraded by it.
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}),
                       reach=_FakeReach(configured=7, reached=7, unreached=[])).run()

    assert envelope.status == 'success'
    assert envelope.errors == []


def test_all_symbols_failed_is_error_but_rows_remain():
    config = _config(['BTCUSD', 'ETHUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': LLMApiError('down'),
                                       'ETHUSD': VectorStoreError('db gone')})).run()
    # Nothing evaluated -> 'error'; the symbol invariant still holds (parseable superset).
    assert envelope.status == 'error'
    assert [r.symbol for r in envelope.result] == ['BTCUSD', 'ETHUSD']
    assert all(r.signal == 'HOLD' for r in envelope.result)
    assert {e.type for e in envelope.errors} == {'LLM_API_ERROR', 'VECTOR_STORE_ERROR'}


def test_cost_usd_is_the_recorders_session_delta():
    recorder = _FakeRecorder()
    recorder.session_usd = 0.005                      # spend from earlier passes

    class _SpendingEvaluator(_FakeEvaluator):
        def evaluate(self, symbol, query):
            recorder.tick()                           # this run's paid call
            return super().evaluate(symbol, query)

    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _SpendingEvaluator({'BTCUSD': _eval('BTCUSD')}), recorder).run()
    assert envelope.metadata.cost_usd == pytest.approx(0.001)   # delta, not the total


def test_run_persists_envelope_with_raw_output():
    # ISSUE_8/36: the pass ends with persistence — envelope + per-symbol raw LLM output.
    store = _FakeStore()
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}), store=store).run()
    assert len(store.saved) == 1
    saved_envelope, raw_output = store.saved[0]
    assert saved_envelope is envelope
    assert raw_output == {'BTCUSD': {'signal': 'BUY'}}
    assert envelope.status == 'success'               # persistence leaves a clean pass clean


def test_no_data_rows_leave_no_raw_output():
    # The no_data shortcut made no LLM call — nothing raw to persist (raw stays None).
    no_data = _eval('LTCUSD')
    no_data.raw_output = {}
    store = _FakeStore()
    _runner(_config(['LTCUSD']), _FakeIngestor(),
            _FakeEvaluator({'LTCUSD': no_data}), store=store).run()
    assert store.saved[0][1] is None


def test_store_failure_degrades_pass_never_kills_it():
    # A dead store must not lose the produced envelope: served anyway, marked partial.
    store = _FakeStore(exc=VectorStoreError('db gone'))
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}), store=store).run()
    assert envelope.status == 'partial'
    assert envelope.errors[-1].type == 'VECTOR_STORE_ERROR'
    assert 'not persisted' in envelope.errors[-1].message


def test_worker_mode_runner_skips_ingest_cleanly():
    # ISSUE_10: ingestor=None = worker mode — acquisition happens on the ingest worker's clock,
    # so this pass touches no source. It used to report *full reach* for exactly that reason
    # (`configured - 0 failures`), making the field a constant in the one mode that ships. Reach
    # now comes from source_health, which the ingest worker keeps writing whoever evaluates.
    config = _config(['BTCUSD'])
    envelope = PipelineRunner(config, None,
                              _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}), _META,
                              llm_model='gpt-4o-mini',
                              source_reach=_FakeReach(configured=2, reached=1,
                                                      unreached=['s2'])).run()
    stages = [t.stage for t in envelope.metadata.stage_timings]
    assert 'fetch' not in stages and 'embed' not in stages   # no Phase A ran
    assert envelope.metadata.sources_configured == 2
    assert envelope.metadata.sources_reached == 1            # measured, though this pass fetched nothing
    assert envelope.metadata.articles_found == 0             # found *this pass*
    # The same state of the world an inline pass degrades on. It used to pass as `success` here
    # purely because this mode has no fetch loop to fail — the mode decided the status, not reality.
    assert envelope.status == 'partial'
    assert envelope.errors[0].type == 'SOURCE_UNREACHABLE'


def test_runner_without_a_reach_reports_zero_not_full():
    # No health store to ask (a caller that never wired one) — the honest answer is "unknown",
    # and 0/0 says that. Defaulting to full reach would be the old lie with a new default.
    config = _config(['BTCUSD'])
    envelope = PipelineRunner(config, None, _FakeEvaluator({'BTCUSD': _eval('BTCUSD')}),
                             _META, llm_model='gpt-4o-mini').run()
    assert envelope.metadata.sources_configured == 0
    assert envelope.metadata.sources_reached == 0


def test_retrieval_funnel_lands_in_metadata():
    # ISSUE_24: the funnel each evaluation carried is assembled per symbol into the
    # envelope metadata — the persisted run can explain a thin or empty context.
    good = _eval('BTCUSD')
    good.retrieval = RetrievalFunnel(in_window=20, floor_dropped=17, near_duplicates=1,
                                     kept=2, best_distance=0.61)
    envelope = _runner(_config(['BTCUSD']), _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': good})).run()
    funnel = envelope.metadata.per_symbol_retrieval['BTCUSD']
    assert (funnel.in_window, funnel.floor_dropped, funnel.kept) == (20, 17, 2)


def test_fanned_config_stamps_variant_hints():
    # ISSUE_42: expansion sets the hints on the config; the runner stamps them into
    # the envelope. Single-model configs (variant_group None) omit the keys entirely.
    config = _config(['BTCUSD']).model_copy(
        update={'pipeline_id': 'p_4o', 'variant_group': 'p', 'variant': '4o'})
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')})).run()
    assert (envelope.metadata.variant_group, envelope.metadata.variant) == ('p', '4o')


def test_taxonomy_fallback_is_partial_response():
    class _Odd(Exception):
        pass
    assert taxonomy_type(_Odd()) == 'PARTIAL_RESPONSE'


def test_format_envelope_run_renders_table_and_footer():
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')})).run()
    text = format_envelope_run(envelope)
    assert '=== Run: p' in text
    assert 'sentiment-crypto@v1 #cafe12345678' in text
    assert 'BTCUSD' in text
    assert '--- run metrics ---' in text              # the shared pattern footer
