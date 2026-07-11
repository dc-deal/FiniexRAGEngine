"""Tests for SymbolEvaluator (ISSUE_6/7) — enrich + timings, no DB/API."""
from datetime import datetime, timezone

import pytest

from finiexragengine.core.pipeline.symbol_evaluator import (
    SymbolEvaluator,
    _compact_prompt,
)
from finiexragengine.exceptions.ragengine_errors import LLMParseError
from finiexragengine.types.article_types import Article
from finiexragengine.types.llm_types import LlmCompletion, LlmUsage
from finiexragengine.types.prompt_metadata import PromptMetadata

_TS = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _article(article_id: str) -> Article:
    return Article(article_id=article_id, source_id='s', source_weight=1.0,
                   url=f'https://example.test/{article_id}', title=f'title {article_id}',
                   summary='summary', language='en', published_at=_TS, fetched_at=_TS)


class _FakeRetriever:
    def __init__(self, articles):
        self._articles = articles

    def retrieve(self, query):
        return self._articles


class _FakeBuilder:
    def build(self, name, prompt_version, symbol, articles):
        return f'PROMPT {symbol} {len(articles)} articles'

    def metadata(self, name, version):
        return PromptMetadata(id=name, version=version, author='', created='',
                              description='', content_hash='deadbeef0000')


class _FakeProvider:
    def __init__(self, data):
        self._data = data

    def complete_structured(self, prompt, json_schema):
        return LlmCompletion(data=self._data, usage=LlmUsage(100, 20))


def _evaluator(articles, data):
    return SymbolEvaluator(_FakeRetriever(articles), _FakeBuilder(), _FakeProvider(data),
                           breaking_threshold=0.8)


def test_enriches_with_provenance_and_times_stages():
    data = {'signal': 'SELL', 'sentiment_score': -0.6, 'confidence': 0.8,
            'reasoning': 'bearish', 'urgency': 0.9}
    ev = _evaluator([_article('a'), _article('b')], data).evaluate('BTCUSD', 'Bitcoin BTC')
    assert ev.result.symbol == 'BTCUSD' and ev.result.signal == 'SELL'
    assert ev.result.is_breaking is True                     # urgency 0.9 >= 0.8
    assert [s.article_id for s in ev.result.sources] == ['a', 'b']   # provenance = retrieved
    assert {t.stage for t in ev.stage_timings} == {'retrieve', 'prompt', 'llm'}
    assert ev.usage.total_tokens == 120
    assert ev.prompt_metadata.content_hash == 'deadbeef0000'   # prompt identity travels along
    assert ev.raw_output == data                # raw model output retained (ISSUE_36)


def test_not_breaking_below_threshold():
    data = {'signal': 'HOLD', 'sentiment_score': 0.0, 'confidence': 0.5,
            'reasoning': 'neutral', 'urgency': 0.3}
    ev = _evaluator([_article('a')], data).evaluate('BTCUSD', 'q')
    assert ev.result.is_breaking is False


def test_bad_output_raises_parse_error():
    data = {'signal': 'MAYBE', 'sentiment_score': 0.0, 'confidence': 0.5,
            'reasoning': 'x', 'urgency': 0.1}
    with pytest.raises(LLMParseError):
        _evaluator([_article('a')], data).evaluate('BTCUSD', 'q')


def test_compact_prompt_collapses_newlines():
    out = _compact_prompt('line1\nline2\nline3', cols=100, lines=5)
    assert 'line1⏎line2⏎line3' in out


# --- empty-context shortcut (ISSUE_24) ---

class _MustNotCallProvider:
    """The shortcut's contract: no context -> the LLM is never touched."""
    def complete_structured(self, prompt, json_schema):
        raise AssertionError('LLM must not be called on empty context')


def test_empty_context_shortcuts_to_no_data_hold():
    evaluator = SymbolEvaluator(_FakeRetriever([]), _FakeBuilder(), _MustNotCallProvider(),
                                breaking_threshold=0.8)
    ev = evaluator.evaluate('LTCUSD', 'Litecoin LTC')
    r = ev.result
    # The contract row, machine-tagged: mechanical HOLD, no evaluation possible.
    assert (r.signal, r.confidence, r.sources) == ('HOLD', 0.0, [])
    assert r.reasoning == 'No relevant news found'
    assert r.basis == 'no_data'
    # Envelope-side proof that no LLM ran: zero tokens, no prompt, empty raw output.
    assert ev.usage.total_tokens == 0 and ev.prompt == '' and ev.raw_output == {}
    assert [t.stage for t in ev.stage_timings] == ['retrieve']   # only retrieval ran
    assert ev.prompt_metadata.content_hash == 'deadbeef0000'     # fingerprint still resolved


def test_llm_path_keeps_default_basis():
    data = {'signal': 'BUY', 'sentiment_score': 0.4, 'confidence': 0.7,
            'reasoning': 'bullish', 'urgency': 0.2}
    ev = _evaluator([_article('a')], data).evaluate('BTCUSD', 'q')
    assert ev.result.basis == 'llm'
