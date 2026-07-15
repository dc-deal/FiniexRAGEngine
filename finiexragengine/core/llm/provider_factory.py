"""Builds a concrete LLM provider from the config's declared `llm.provider` (name -> class)."""
from typing import Optional

from finiexragengine.core.llm.abstract_llm_provider import AbstractLLMProvider
from finiexragengine.core.llm.openai_provider import OpenAIProvider
from finiexragengine.core.observability.budget_guard import BudgetGuard
from finiexragengine.core.observability.cost_recorder import CostRecorder
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import LlmConfig


def build_provider(config: LlmConfig, model: str,
                   cost_recorder: Optional[CostRecorder] = None,
                   section: str = 'llm_eval',
                   pipeline_id: Optional[str] = None,
                   budget_guard: Optional[BudgetGuard] = None) -> AbstractLLMProvider:
    """Instantiate the LLM provider implementation for `config.provider`.

    The provider seam (`AbstractLLMProvider`) keeps the eval flow implementation-agnostic;
    this factory is the single point where a name becomes a class — the mirror of
    `source_factory`. Note that OpenAI-compatible endpoints (vLLM, Ollama, fine-tunes)
    are still `provider: 'openai'` — they ride the same class via `llm.base_url` /
    the model string; a *new* entry here means a genuinely different API protocol.
    An unknown name fails loudly at assembly, before any call is made.
    """
    if config.provider == 'openai':
        return OpenAIProvider(config, model, cost_recorder=cost_recorder,
                              section=section, pipeline_id=pipeline_id,
                              budget_guard=budget_guard)
    raise ConfigurationError(
        f"llm.provider '{config.provider}' is not implemented "
        "(available: 'openai' — OpenAI-compatible endpoints use base_url instead)")
