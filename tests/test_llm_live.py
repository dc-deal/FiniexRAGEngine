"""Live (paid) test — real OpenAI structured sentiment output (ISSUE_6).

Fenced behind the `paid` marker (excluded from default runs). Run deliberately:

    pytest -m paid tests/test_llm_live.py -v

Needs OPENAI_API_KEY. One gpt-4o-mini call — fractions of a cent.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip('openai')

from finiexragengine.core.llm.openai_provider import OpenAIProvider  # noqa: E402
from finiexragengine.core.llm.prompt_builder import PromptBuilder  # noqa: E402
from finiexragengine.types.article_types import Article  # noqa: E402
from finiexragengine.types.config_types.app_config_types import LlmConfig  # noqa: E402
from finiexragengine.types.outcome_types import SentimentLlmOutput  # noqa: E402

pytestmark = [
    pytest.mark.paid,
    pytest.mark.skipif(not os.environ.get('OPENAI_API_KEY'),
                       reason='OPENAI_API_KEY not set'),
]

_PROMPTS = Path(__file__).resolve().parents[1] / 'prompts'


def _article(article_id: str, title: str, summary: str) -> Article:
    now = datetime.now(timezone.utc)
    return Article(article_id=article_id, source_id='decrypt', source_weight=1.0,
                   url=f'https://example.test/{article_id}', title=title, summary=summary,
                   language='en', published_at=now, fetched_at=now)


def test_live_structured_sentiment_conforms_and_captures_usage():
    articles = [
        _article('a', 'Bitcoin surges to new high as ETF inflows accelerate',
                 'Spot bitcoin ETFs saw record inflows; BTC broke resistance, analysts turn bullish.'),
        _article('b', 'Bitcoin dominance rises as altcoins bleed',
                 'BTC held firm while altcoins dropped, a risk-off rotation into bitcoin.'),
    ]
    prompt = PromptBuilder(_PROMPTS).build('sentiment', '1', 'Bitcoin BTC', articles)
    result = OpenAIProvider(LlmConfig(), 'gpt-4o-mini').complete_structured(
        prompt, SentimentLlmOutput.model_json_schema())

    parsed = SentimentLlmOutput(**result.data)          # conforms to the field schema
    assert parsed.signal in ('BUY', 'SELL', 'HOLD')
    assert -1.0 <= parsed.sentiment_score <= 1.0
    assert result.usage.total_tokens > 0                # usage captured for cost
    assert result.model.startswith('gpt-4o-mini')       # served snapshot reported
