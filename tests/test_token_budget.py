"""TokenBudget — fitting text to the embedding model's input limit (ISSUE_79).

Pure CPU, no API: tiktoken is a local tokenizer. These are the tests that make the truncation
counts trustworthy, because those counts are written to the corpus as an analysis field.
"""
import pytest

from finiexragengine.core.rag.token_budget import TokenBudget
from finiexragengine.exceptions.ragengine_errors import EmbeddingError
from finiexragengine.types.config_types.app_config_types import EmbeddingConfig


def _budget(**overrides) -> TokenBudget:
    return TokenBudget(EmbeddingConfig(**overrides))


def test_short_text_passes_through_but_is_still_counted():
    fitted = _budget().fit('Bitcoin rallies on ETF news.')
    assert fitted.dropped_tokens is None          # untouched
    assert fitted.text == 'Bitcoin rallies on ETF news.'
    assert fitted.tokens > 0                      # the count is reported either way


def test_over_long_text_is_cut_to_exactly_the_limit():
    budget = _budget(max_input_tokens=50)
    fitted = budget.fit('token ' * 500)
    assert fitted.tokens == 50
    assert fitted.dropped_tokens == pytest.approx(500 - 50, abs=5)   # ~1 token per 'token '
    assert len(fitted.text) < len('token ' * 500)


def test_the_cut_lands_on_a_token_boundary():
    """The reason truncation happens in token space rather than by characters.

    A character-level cut can slice a token in half; the tail then re-tokenizes differently than
    it was counted, and the result can land back over the limit — the very failure being fixed.
    Re-fitting an already-fitted text must therefore be a no-op.
    """
    budget = _budget(max_input_tokens=40)
    once = budget.fit('Ethereum ' * 300)
    twice = budget.fit(once.text)
    assert twice.tokens <= 40
    assert twice.dropped_tokens is None           # already fits — nothing more to cut


def test_a_text_exactly_at_the_limit_is_not_truncated():
    budget = _budget(max_input_tokens=40)
    at_limit = budget.fit('word ' * 200).text     # produces exactly 40 tokens
    assert budget.fit(at_limit).dropped_tokens is None


def test_an_unknown_model_fails_at_construction_not_mid_pass():
    # The whole point of resolving eagerly: a model tiktoken cannot map must surface at boot,
    # where the startup check reports it — not inside a worker thread on the first article.
    with pytest.raises(EmbeddingError) as exc:
        _budget(model='some-future-embedding-model-v9')
    assert 'tokenizer' in str(exc.value)
    assert 'encoding' in str(exc.value)            # points at the config escape hatch


def test_the_encoding_override_wins_over_the_model_lookup():
    # The escape hatch for a model shipped before tiktoken knows it.
    budget = _budget(model='some-future-embedding-model-v9', encoding='cl100k_base')
    assert budget.fit('hello').tokens > 0


def test_counting_is_not_a_character_heuristic():
    """Why the dependency exists at all.

    "~4 characters per token" holds for English prose and collapses on dense punctuation and
    non-Latin scripts — exactly the content that blew the 8192-token limit in production. If a
    character estimate were good enough, these two texts of equal length would count alike.
    """
    budget = _budget()
    length = 800
    prose = ('the quick brown fox jumps over the lazy dog ' * 40)[:length]
    dense = ('https://example.test/a/b?c=1&d=2#e ' * 40)[:length]
    assert len(prose) == len(dense) == length      # identical character budget …

    prose_tokens = budget.fit(prose).tokens
    dense_tokens = budget.fit(dense).tokens
    # … and a very different token cost. A chars-per-token rule tuned on the first would
    # under-count the second and let it sail past the limit — which is how the 400 happened.
    assert dense_tokens > prose_tokens * 1.5, (prose_tokens, dense_tokens)
