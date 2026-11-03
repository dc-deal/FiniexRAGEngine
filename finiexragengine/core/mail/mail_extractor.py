"""The extract step's one call (#138): a stored mail's text → English stories.

A separate step from intake by decision (operator, 2026-11-03): the raw mail is already stored when
this runs, so a provider outage leaves a backlog instead of a lost mail, and a new instruction version
is a re-run over stored text. The call is an ordinary structured completion through the provider seam —
budget guard, retry loop, one `cost_log` row per attempt under section `mail_extract` — and the
instruction is a versioned template like every prompt, its body hash recorded beside each extract.
"""
import time
from dataclasses import dataclass, field
from typing import List, Optional

from pydantic import ValidationError

from finiexragengine.core.llm.abstract_llm_provider import AbstractLLMProvider
from finiexragengine.core.llm.prompt_builder import PromptBuilder
from finiexragengine.exceptions.ragengine_errors import LLMError
from finiexragengine.types.config_types.app_config_types import MailExtractConfig, ModelPrice
from finiexragengine.types.mail_types import MailExtraction, MailStory, PendingMail


@dataclass(frozen=True)
class ExtractOutcome:
    stories: List[MailStory]
    dropped: List[str]
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    usd: Optional[float]
    served_model: str = ''
    notes: List[str] = field(default_factory=list)


class MailExtractor:
    """Turns one stored mail into stories with the configured instruction and model."""

    def __init__(self, provider: AbstractLLMProvider, prompts: PromptBuilder,
                 config: MailExtractConfig, price: Optional[ModelPrice]) -> None:
        self._provider = provider
        self._prompts = prompts
        self._config = config
        self._price = price
        # Resolved once: a missing template fails at assembly, not on the first mail.
        self._prompt_hash = prompts.metadata(config.prompt_name, str(config.prompt_version)).content_hash

    @property
    def version(self) -> int:
        return self._config.prompt_version

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def prompt_hash(self) -> str:
        return self._prompt_hash

    def extract(self, mail: PendingMail) -> ExtractOutcome:
        prompt = self._prompts.render(self._config.prompt_name, str(self._config.prompt_version),
                                      text=mail.text)
        started = time.monotonic()
        completion = self._provider.complete_structured(prompt, MailExtraction.model_json_schema())
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            parsed = MailExtraction(**completion.data)
        except ValidationError as exc:
            raise LLMError(f'mail extract: answer does not match the schema ({exc.error_count()} errors)') from exc
        usage = completion.usage
        usd = (round(usage.prompt_tokens / 1000 * self._price.input_per_1k
                     + usage.completion_tokens / 1000 * self._price.output_per_1k, 6)
               if self._price is not None else None)
        return ExtractOutcome(stories=[story for story in parsed.items
                                       if story.headline.strip() and story.facts.strip()],
                              dropped=parsed.dropped, prompt_tokens=usage.prompt_tokens,
                              completion_tokens=usage.completion_tokens, duration_ms=duration_ms,
                              usd=usd, served_model=completion.model)
