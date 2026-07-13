"""Tests for the PromptBuilder (ISSUE_6) — Jinja2 .md template fill + versioning, no API."""
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip('jinja2')

from finiexragengine.core.llm.prompt_builder import PromptBuilder  # noqa: E402
from finiexragengine.exceptions.ragengine_errors import LLMError  # noqa: E402
from finiexragengine.types.article_types import Article  # noqa: E402

_TS = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _family(tmp_path):
    """Prompt-family folder (prompts/<name>/<name>_v<N>.md layout)."""
    folder = tmp_path / 'sentiment'
    folder.mkdir(exist_ok=True)
    return folder


def _article(article_id: str, title: str, summary: str) -> Article:
    return Article(article_id=article_id, source_id='decrypt', source_weight=1.0,
                   url=f'https://example.test/{article_id}', title=title, summary=summary,
                   language='en', published_at=_TS, fetched_at=_TS)


def test_build_fills_symbol_and_loops_articles(tmp_path):
    (_family(tmp_path) / 'sentiment_v1.md').write_text(
        'Symbol: {{ symbol }}\n'
        '{% for a in articles %}[{{ loop.index }}] {{ a.source_id }} {{ a.title }} — {{ a.summary }}\n'
        '{% endfor %}', encoding='utf-8')
    prompt = PromptBuilder(tmp_path).build(
        'sentiment', '1', 'BTCUSD', [_article('a', 'BTC rallies', 'bitcoin jumps 5%')])
    assert 'Symbol: BTCUSD' in prompt
    assert '[1] decrypt BTC rallies — bitcoin jumps 5%' in prompt


def test_empty_articles_conditional(tmp_path):
    (_family(tmp_path) / 'sentiment_v1.md').write_text(
        '{% if articles %}HAVE{% else %}(no relevant articles){% endif %}', encoding='utf-8')
    assert 'no relevant articles' in PromptBuilder(tmp_path).build('sentiment', '1', 'BTCUSD', [])


def test_prompt_version_selects_the_file(tmp_path):
    (_family(tmp_path) / 'sentiment_v1.md').write_text('v1 {{ symbol }}', encoding='utf-8')
    (_family(tmp_path) / 'sentiment_v2.md').write_text('v2 {{ symbol }}', encoding='utf-8')
    assert PromptBuilder(tmp_path).build('sentiment', '2', 'ETH', []).startswith('v2')


def test_missing_template_raises_llmerror(tmp_path):
    with pytest.raises(LLMError):
        PromptBuilder(tmp_path).build('sentiment', '9', 'BTCUSD', [])


def test_article_braces_are_not_reparsed(tmp_path):
    # A rendered value containing braces must not be re-evaluated as template syntax.
    (_family(tmp_path) / 'sentiment_v1.md').write_text(
        '{% for a in articles %}{{ a.summary }}{% endfor %}', encoding='utf-8')
    prompt = PromptBuilder(tmp_path).build(
        'sentiment', '1', 'BTCUSD', [_article('a', 'title', 'weird {{ not_a_field }}')])
    assert 'weird {{ not_a_field }}' in prompt


# --- front-matter metadata (ISSUE_33) ---

def test_front_matter_parsed_and_not_rendered(tmp_path):
    (_family(tmp_path) / 'sentiment_v1.md').write_text(
        '---\n'
        'id: sentiment-crypto\n'
        'version: 1\n'
        'author: Team\n'
        'created: 2026-07-09\n'
        'description: test prompt\n'
        '---\n'
        'Symbol: {{ symbol }}\n', encoding='utf-8')
    builder = PromptBuilder(tmp_path)
    meta = builder.metadata('sentiment', '1')
    assert (meta.id, meta.version, meta.author) == ('sentiment-crypto', '1', 'Team')
    assert (meta.created, meta.description) == ('2026-07-09', 'test prompt')
    # The `---` block must not leak into the rendered prompt.
    prompt = builder.build('sentiment', '1', 'BTCUSD', [])
    assert prompt.strip() == 'Symbol: BTCUSD'
    assert 'id:' not in prompt


def test_body_hash_tracks_body_not_metadata(tmp_path):
    # Same body, differing metadata -> same fingerprint; changed body -> moved fingerprint.
    (tmp_path / 'p').mkdir()
    (tmp_path / 'p' / 'p_v1.md').write_text('---\nid: p\nauthor: A\n---\nBODY A\n', encoding='utf-8')
    (tmp_path / 'p' / 'p_v2.md').write_text('---\nid: p\nauthor: B\n---\nBODY A\n', encoding='utf-8')
    (tmp_path / 'p' / 'p_v3.md').write_text('---\nid: p\nauthor: A\n---\nBODY CHANGED\n', encoding='utf-8')
    builder = PromptBuilder(tmp_path)
    h1 = builder.metadata('p', '1').content_hash
    h2 = builder.metadata('p', '2').content_hash
    h3 = builder.metadata('p', '3').content_hash
    assert h1 == h2       # only the author (metadata) differs
    assert h1 != h3       # the body changed -> silent edit is visible


def test_missing_front_matter_defaults(tmp_path):
    (_family(tmp_path) / 'sentiment_v1.md').write_text('No front matter {{ symbol }}', encoding='utf-8')
    meta = PromptBuilder(tmp_path).metadata('sentiment', '1')
    assert meta.id == 'sentiment'       # falls back to the file name
    assert meta.version == '1'          # falls back to the requested version
    assert meta.author == ''


# --- render context: `now` + newest-first sorting (prompt v2 features) ---

def test_now_is_available_to_the_template(tmp_path):
    (_family(tmp_path) / 'sentiment_v1.md').write_text(
        "Current time: {{ now.strftime('%Y') }}", encoding='utf-8')
    prompt = PromptBuilder(tmp_path).build('sentiment', '1', 'BTCUSD', [])
    assert f'Current time: {datetime.now(timezone.utc).year}' in prompt


def test_template_can_sort_articles_newest_first(tmp_path):
    # The ordering is template-owned (versioned, hash-visible) — the engine hands the
    # articles over in retrieval rank order; v2 re-sorts them by published_at.
    (_family(tmp_path) / 'sentiment_v1.md').write_text(
        "{% for a in articles|sort(attribute='published_at', reverse=true) %}"
        '{{ a.article_id }} {% endfor %}', encoding='utf-8')
    older = _article('older', 'o', 's')
    newer = Article(article_id='newer', source_id='decrypt', source_weight=1.0,
                    url='https://example.test/newer', title='n', summary='s',
                    language='en', published_at=_TS + timedelta(hours=3), fetched_at=_TS)
    prompt = PromptBuilder(tmp_path).build('sentiment', '1', 'BTCUSD', [older, newer])
    assert prompt.strip() == 'newer older'
