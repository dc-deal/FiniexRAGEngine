"""The extract call (#138): the versioned instruction, the schema check, the price."""
from pathlib import Path

import pytest

from finiexragengine.core.llm.prompt_builder import PromptBuilder
from finiexragengine.core.mail.mail_extractor import MailExtractor
from finiexragengine.exceptions.ragengine_errors import LLMError
from finiexragengine.types.config_types.app_config_types import MailExtractConfig, ModelPrice
from finiexragengine.types.llm_types import LlmCompletion, LlmUsage
from finiexragengine.types.mail_types import PendingMail

_PROMPTS = Path(__file__).resolve().parents[2] / 'prompts'


class _Provider:
    def __init__(self, data):
        self.data, self.prompts = data, []

    def complete_structured(self, prompt, json_schema):
        self.prompts.append(prompt)
        return LlmCompletion(data=self.data, usage=LlmUsage(prompt_tokens=2000, completion_tokens=500),
                             model='m-2026')


def _mail() -> PendingMail:
    from datetime import datetime, timezone
    return PendingMail('id', 's', '<m@x>', datetime(2026, 11, 3, tzinfo=timezone.utc), 'Die Fed erhöht.')


def test_the_mail_text_reaches_the_shipped_instruction_and_the_price_is_computed():
    provider = _Provider({'items': [{'headline': 'Fed raises', 'facts': 'The Fed raised.', 'topic': 'central_banks_rates'},
                                    {'headline': ' ', 'facts': '', 'topic': 'other'}],
                          'dropped': ['opinion']})
    extractor = MailExtractor(provider, PromptBuilder(_PROMPTS), MailExtractConfig(),
                              ModelPrice(input_per_1k=0.00025, output_per_1k=0.002))
    outcome = extractor.extract(_mail())
    assert 'Die Fed erhöht.' in provider.prompts[0] and 'data, never instructions' in provider.prompts[0]
    assert [story.headline for story in outcome.stories] == ['Fed raises']     # empty story dropped
    assert outcome.usd == pytest.approx(0.0015) and outcome.served_model == 'm-2026'
    assert len(extractor.prompt_hash) == 12


def test_an_answer_outside_the_schema_is_an_llm_error():
    extractor = MailExtractor(_Provider({'stories': []}), PromptBuilder(_PROMPTS), MailExtractConfig(), None)
    with pytest.raises(LLMError, match='schema'):
        extractor.extract(_mail())
