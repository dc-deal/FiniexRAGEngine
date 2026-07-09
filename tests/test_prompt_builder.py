"""Tests for the PromptBuilder (ISSUE_6) — Jinja2 .md template fill + versioning, no API."""
from datetime import datetime, timezone

import pytest

pytest.importorskip('jinja2')

from finiexragengine.core.llm.prompt_builder import PromptBuilder  # noqa: E402
from finiexragengine.exceptions.ragengine_errors import LLMError  # noqa: E402
from finiexragengine.types.article_types import Article  # noqa: E402

_TS = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _article(article_id: str, title: str, summary: str) -> Article:
    return Article(article_id=article_id, source_id='decrypt', source_weight=1.0,
                   url=f'https://example.test/{article_id}', title=title, summary=summary,
                   language='en', published_at=_TS, fetched_at=_TS)


def test_build_fills_symbol_and_loops_articles(tmp_path):
    (tmp_path / 'sentiment_v1.md').write_text(
        'Symbol: {{ symbol }}\n'
        '{% for a in articles %}[{{ loop.index }}] {{ a.source_id }} {{ a.title }} — {{ a.summary }}\n'
        '{% endfor %}', encoding='utf-8')
    prompt = PromptBuilder(tmp_path).build(
        'sentiment', '1', 'BTCUSD', [_article('a', 'BTC rallies', 'bitcoin jumps 5%')])
    assert 'Symbol: BTCUSD' in prompt
    assert '[1] decrypt BTC rallies — bitcoin jumps 5%' in prompt


def test_empty_articles_conditional(tmp_path):
    (tmp_path / 'sentiment_v1.md').write_text(
        '{% if articles %}HAVE{% else %}(no relevant articles){% endif %}', encoding='utf-8')
    assert 'no relevant articles' in PromptBuilder(tmp_path).build('sentiment', '1', 'BTCUSD', [])


def test_prompt_version_selects_the_file(tmp_path):
    (tmp_path / 'sentiment_v1.md').write_text('v1 {{ symbol }}', encoding='utf-8')
    (tmp_path / 'sentiment_v2.md').write_text('v2 {{ symbol }}', encoding='utf-8')
    assert PromptBuilder(tmp_path).build('sentiment', '2', 'ETH', []).startswith('v2')


def test_missing_template_raises_llmerror(tmp_path):
    with pytest.raises(LLMError):
        PromptBuilder(tmp_path).build('sentiment', '9', 'BTCUSD', [])


def test_article_braces_are_not_reparsed(tmp_path):
    # A rendered value containing braces must not be re-evaluated as template syntax.
    (tmp_path / 'sentiment_v1.md').write_text(
        '{% for a in articles %}{{ a.summary }}{% endfor %}', encoding='utf-8')
    prompt = PromptBuilder(tmp_path).build(
        'sentiment', '1', 'BTCUSD', [_article('a', 'title', 'weird {{ not_a_field }}')])
    assert 'weird {{ not_a_field }}' in prompt
