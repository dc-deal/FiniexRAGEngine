"""Tests for the pipeline-declared prompt block (ISSUE_33) + required eval model."""
import pytest
from pydantic import ValidationError

from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


def _base(**extra):
    cfg = {'pipeline_id': 'p', 'outcome_type': 'o', 'market': 'crypto',
           'symbols': ['BTCUSD'], 'source_set': 'test_news',
           'llm': {'model': 'gpt-4o-mini'}}
    cfg.update(extra)
    return cfg


def test_prompt_block_parsed():
    cfg = PipelineConfig(**_base(prompt={'name': 'sentiment', 'version': '2'}))
    assert cfg.prompt.name == 'sentiment'
    assert cfg.prompt.version == '2'


def test_prompt_defaults_when_absent():
    # Default points at a real family folder (prompts/crypto_sentiment/) — a config
    # omitting the block still resolves, it never silently gets a foreign wording.
    cfg = PipelineConfig(**_base())
    assert cfg.prompt.name == 'crypto_sentiment'
    assert cfg.prompt.version == '1'


def test_llm_model_is_required():
    # The eval model is series-defining: a constellation must declare it — no silent
    # inheritance from a global default (fails at load, not mid-run).
    data = _base()
    del data['llm']
    with pytest.raises(ValidationError):
        PipelineConfig(**data)


def test_llm_model_parsed():
    cfg = PipelineConfig(**_base(llm={'model': 'gpt-4o'}))
    assert cfg.llm.model == 'gpt-4o'
