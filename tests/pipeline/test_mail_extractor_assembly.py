"""The extract step's one construction path (#138 addendum) — the service and a separate run share it."""
import pytest

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import AppConfig, MailboxConfig


def _assembler(config: AppConfig) -> PipelineAssembler:
    # __new__ + manual wiring skips every DB connect — the builder needs config, prompts and a recorder.
    assembler = PipelineAssembler.__new__(PipelineAssembler)
    assembler._cfg = config
    assembler._app = AppConfigManager()
    assembler._recorder = None
    assembler._budget_guard = None
    return assembler


def test_an_unconfigured_source_or_a_model_outside_the_allowlist_is_refused():
    config = AppConfig()
    with pytest.raises(ConfigurationError, match='no `mail.mailboxes.newsletter_a` block'):
        _assembler(config).build_mail_extractor('newsletter_a')
    config.llm.allowed_models = ['gpt-4o-mini']
    config.mail.mailboxes['newsletter_a'] = MailboxConfig()
    with pytest.raises(ConfigurationError, match='not in llm.allowed_models'):
        _assembler(config).build_mail_extractor('newsletter_a')


def test_a_configured_source_gets_the_versioned_instruction():
    config = AppConfig()
    config.mail.mailboxes['newsletter_a'] = MailboxConfig()
    extractor = _assembler(config).build_mail_extractor('newsletter_a')
    assert (extractor.version, extractor.model, len(extractor.prompt_hash)) == (2, 'gpt-5-mini', 12)
