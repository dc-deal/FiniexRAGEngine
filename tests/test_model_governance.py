"""Tests for the model-governance gate (allowed_models) — no DB, no API.

The assembler's `resolve_model` is the choke point: a pipeline's declared model must be
inside `app_config.llm.allowed_models`, checked at assembly — before any spend.
"""
import pytest

from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import AppConfig
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


class _FakeApp:
    """AppConfigManager stand-in — resolve_model only touches get_config()."""
    def __init__(self, cfg: AppConfig):
        self._cfg = cfg

    def get_config(self) -> AppConfig:
        return self._cfg


def _assembler(allowed) -> PipelineAssembler:
    cfg = AppConfig(llm={'allowed_models': allowed})
    # __new__ + manual wiring skips the CostRecorder DB connect — resolve_model
    # needs only the config.
    assembler = PipelineAssembler.__new__(PipelineAssembler)
    assembler._app = _FakeApp(cfg)
    assembler._cfg = cfg
    return assembler


def _pipeline(model: str) -> PipelineConfig:
    return PipelineConfig(
        pipeline_id='p', outcome_type='o', market='crypto', symbols=['BTCUSD'],
        llm={'model': model}, sources=[{'source_id': 's', 'url': 'http://x'}])


def test_allowed_model_resolves():
    assert _assembler(['gpt-4o-mini']).resolve_model(_pipeline('gpt-4o-mini')) == 'gpt-4o-mini'


def test_unlisted_model_fails_fast():
    with pytest.raises(ConfigurationError) as exc:
        _assembler(['gpt-4o-mini']).resolve_model(_pipeline('gpt-4o'))
    assert 'allowed_models' in str(exc.value)


def test_fine_tune_id_works_once_allowlisted():
    # Custom models are just model strings: allowlist the ft-id (user_configs) and go.
    ft = 'ft:gpt-4o-mini-2024-07-18:acme::abc123'
    assert _assembler(['gpt-4o-mini', ft]).resolve_model(_pipeline(ft)) == ft
