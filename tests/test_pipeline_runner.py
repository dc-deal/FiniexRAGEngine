"""Tests for the PipelineRunner (ISSUE_7) — envelope invariants, no DB/API.

Fakes sit at the runner's injection seam (Ingestor / SymbolEvaluator), so these tests
exercise orchestration + assembly only: every-symbol-present, partial-over-error, the
RunError taxonomy, metric capture and the prompt fingerprint (ISSUE_33).
"""
from datetime import datetime, timezone
from typing import List

import pytest

from finiexragengine.core.pipeline.ingestor import IngestResult
from finiexragengine.core.pipeline.pipeline_runner import (
    PipelineRunner,
    format_envelope_run,
    taxonomy_type,
)
from finiexragengine.core.pipeline.symbol_evaluator import SymbolEval
from finiexragengine.exceptions.ragengine_errors import (
    LLMApiError,
    LLMTimeoutError,
    VectorStoreError,
)
from finiexragengine.types.article_types import Article
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.llm_types import LlmUsage
from finiexragengine.types.outcome_types import SentimentResult, StageTiming
from finiexragengine.types.prompt_metadata import PromptMetadata

_TS = datetime(2026, 7, 1, tzinfo=timezone.utc)
_META = PromptMetadata(id='sentiment-crypto', version='1', author='t', created='',
                       description='', content_hash='cafe12345678')


def _config(symbols: List[str]) -> PipelineConfig:
    return PipelineConfig(
        pipeline_id='p', outcome_type='sentiment_fear_greed', market='crypto',
        symbols=symbols, sources=[{'source_id': 's1', 'url': 'http://x'},
                                  {'source_id': 's2', 'url': 'http://y'}])


def _article(article_id: str) -> Article:
    return Article(article_id=article_id, source_id='s1', source_weight=1.0,
                   url=f'https://example.test/{article_id}', title='t', summary='s',
                   language='en', published_at=_TS, fetched_at=_TS)


def _timing(stage: str, ms: float) -> StageTiming:
    return StageTiming(stage=stage, started_at=_TS, ended_at=_TS, duration_ms=ms)


def _eval(symbol: str, tokens=(100, 20)) -> SymbolEval:
    result = SentimentResult(symbol=symbol, signal='BUY', sentiment_score=0.5,
                             confidence=0.8, reasoning='bullish')
    return SymbolEval(result=result, prompt='P', prompt_metadata=_META,
                      usage=LlmUsage(*tokens), articles=[_article('a')],
                      stage_timings=[_timing('retrieve', 10.0), _timing('llm', 90.0)],
                      raw_output={'signal': 'BUY'})


class _FakeIngestor:
    def __init__(self, failed=None):
        self._failed = failed or {}

    def run(self) -> IngestResult:
        return IngestResult(fetched=10, embedded=4, stored=4,
                            failed_sources=dict(self._failed),
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


def _runner(config, ingestor, evaluator, recorder=None):
    return PipelineRunner(config, ingestor, evaluator, _META,
                          llm_model='gpt-4o-mini', cost_recorder=recorder)


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
    stages = [t.stage for t in envelope.metadata.stage_timings]
    assert stages == ['fetch', 'embed', 'retrieve', 'llm', 'retrieve', 'llm']
    assert envelope.metadata.processing_time_ms > 0.0


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
    assert [e.type for e in envelope.errors] == ['LLM_TIMEOUT']


def test_failed_source_records_taxonomy_and_partial():
    config = _config(['BTCUSD'])
    envelope = _runner(config, _FakeIngestor(failed={'s2': 'connection refused'}),
                       _FakeEvaluator({'BTCUSD': _eval('BTCUSD')})).run()
    assert envelope.status == 'partial'
    assert envelope.metadata.sources_reached == 1     # 2 configured, 1 failed
    assert envelope.errors[0].type == 'SOURCE_UNREACHABLE'
    assert 's2' in envelope.errors[0].message


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
