"""Tests for the pipeline-declared prompt block (ISSUE_33)."""
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


def _base(**extra):
    cfg = {'pipeline_id': 'p', 'outcome_type': 'o', 'market': 'crypto',
           'symbols': ['BTCUSD'], 'sources': [{'source_id': 's', 'url': 'http://x'}]}
    cfg.update(extra)
    return cfg


def test_prompt_block_parsed():
    cfg = PipelineConfig(**_base(prompt={'name': 'sentiment', 'version': '2'}))
    assert cfg.prompt.name == 'sentiment'
    assert cfg.prompt.version == '2'


def test_prompt_defaults_when_absent():
    cfg = PipelineConfig(**_base())
    assert cfg.prompt.name == 'sentiment'
    assert cfg.prompt.version == '1'
