"""Multi-model variant fan-out (ISSUE_42) — config validation, registry expansion,
envelope hints. No DB, no API. Format A, as confirmed with the Testing IDE: a variant
stream is an ordinary pipeline_id; the default variant keeps the bare constellation id.
"""
import json

import pytest
from pydantic import ValidationError

from finiexragengine.core.pipeline.pipeline_registry import (
    PipelineRegistry,
    expand_variants,
)
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.pipeline_config_types import (
    PipelineConfig,
    PipelineLlmConfig,
)
from finiexragengine.types.outcome_types import RunMetadata


def _config(llm: dict) -> PipelineConfig:
    return PipelineConfig(
        pipeline_id='crypto_sentiment', outcome_type='sentiment_fear_greed',
        market='crypto', symbols=[{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}], llm=llm,
        source_set='crypto_news')


_FAN = {'models': [
    {'name': 'gpt-4o-mini', 'sub_pipeline_id': 'mini', 'default': True},
    {'name': 'gpt-4o', 'sub_pipeline_id': '4o_enhanced'},
]}


# --- config validation: exactly one form, exactly one default, safe ids --------------

def test_single_model_form_still_valid():
    assert _config({'model': 'gpt-4o-mini'}).llm.model == 'gpt-4o-mini'


def test_model_and_models_are_mutually_exclusive():
    with pytest.raises(ValidationError, match='exactly one'):
        PipelineLlmConfig(model='gpt-4o-mini', models=_FAN['models'])
    with pytest.raises(ValidationError, match='exactly one'):
        PipelineLlmConfig()                       # neither — the model stays REQUIRED (#40)


def test_exactly_one_default_variant():
    with pytest.raises(ValidationError, match='default'):
        PipelineLlmConfig(models=[
            {'name': 'a', 'sub_pipeline_id': 'x'},
            {'name': 'b', 'sub_pipeline_id': 'y'}])
    with pytest.raises(ValidationError, match='default'):
        PipelineLlmConfig(models=[
            {'name': 'a', 'sub_pipeline_id': 'x', 'default': True},
            {'name': 'b', 'sub_pipeline_id': 'y', 'default': True}])


def test_sub_ids_unique_and_charset_safe():
    with pytest.raises(ValidationError, match='unique'):
        PipelineLlmConfig(models=[
            {'name': 'a', 'sub_pipeline_id': 'x', 'default': True},
            {'name': 'b', 'sub_pipeline_id': 'x'}])
    with pytest.raises(ValidationError, match='a-z0-9_'):
        PipelineLlmConfig(models=[
            {'name': 'a', 'sub_pipeline_id': '4o-Enhanced!', 'default': True}])


# --- registry expansion: format A stream ids + hints ---------------------------------

def test_single_model_config_passes_through_unexpanded():
    config = _config({'model': 'gpt-4o-mini'})
    assert expand_variants(config) == [config]
    assert config.variant_group is None and config.variant is None


def test_fan_expands_to_streams_with_hints():
    streams = expand_variants(_config(_FAN))
    by_id = {c.pipeline_id: c for c in streams}
    # Default keeps the bare historical id; the other concatenates (format A).
    assert set(by_id) == {'crypto_sentiment', 'crypto_sentiment_4o_enhanced'}
    default, enhanced = by_id['crypto_sentiment'], by_id['crypto_sentiment_4o_enhanced']
    # Each logical pipeline is a plain single-model config downstream.
    assert default.llm.model == 'gpt-4o-mini' and default.llm.models is None
    assert enhanced.llm.model == 'gpt-4o'
    # EVERY variant carries both hints — including the default.
    assert default.variant_group == enhanced.variant_group == 'crypto_sentiment'
    assert (default.variant, enhanced.variant) == ('mini', '4o_enhanced')
    # Everything else is shared: same symbols, sources, prompt, retrieval.
    assert default.symbols == enhanced.symbols and default.prompt == enhanced.prompt


def test_disabled_variant_is_defined_but_not_expanded():
    # `enabled: false` keeps the variant in the config but produces no stream (no cost).
    streams = expand_variants(_config({'models': [
        {'name': 'gpt-4o-mini', 'sub_pipeline_id': 'mini', 'default': True},
        {'name': 'gpt-4o', 'sub_pipeline_id': '4o_enhanced', 'enabled': False}]}))
    assert [c.pipeline_id for c in streams] == ['crypto_sentiment']   # only the default runs


def test_disabling_the_default_variant_is_rejected():
    # The default owns the bare pipeline_id — disabling it would leave no default stream.
    with pytest.raises(ValidationError, match='default variant cannot be disabled'):
        PipelineLlmConfig(models=[
            {'name': 'a', 'sub_pipeline_id': 'mini', 'default': True, 'enabled': False},
            {'name': 'b', 'sub_pipeline_id': '4o'}])


def test_registry_loads_fan_as_addressable_streams(tmp_path):
    data = _config(_FAN).model_dump(exclude_none=True)
    (tmp_path / 'crypto_sentiment.json').write_text(json.dumps(data))
    registry = PipelineRegistry(tmp_path)
    registry.load()
    # A variant stream is an ordinary pipeline_id — CLIs/API address it as-is.
    assert registry.get('crypto_sentiment').get_config().llm.model == 'gpt-4o-mini'
    assert registry.get('crypto_sentiment_4o_enhanced').get_config().llm.model == 'gpt-4o'
    # The model check's used_by loop sees every variant model for free.
    models = {p.get_config().pipeline_id: p.get_config().llm.model
              for p in registry.list_pipelines()}
    assert models == {'crypto_sentiment': 'gpt-4o-mini',
                      'crypto_sentiment_4o_enhanced': 'gpt-4o'}


def test_registry_refuses_stream_id_collision(tmp_path):
    # A file whose id equals a derived stream id would shadow a signal series.
    (tmp_path / 'a_fan.json').write_text(json.dumps(
        _config(_FAN).model_dump(exclude_none=True)))
    collider = _config({'model': 'gpt-4o-mini'}).model_copy(
        update={'pipeline_id': 'crypto_sentiment_4o_enhanced'})
    (tmp_path / 'z_collider.json').write_text(json.dumps(
        collider.model_dump(exclude_none=True)))
    registry = PipelineRegistry(tmp_path)
    with pytest.raises(ConfigurationError, match='duplicate pipeline_id'):
        registry.load()


# --- envelope hints: present on fan streams, absent keys otherwise -------------------

def test_hints_serialize_only_when_set():
    fanned = RunMetadata(model='gpt-4o', variant_group='crypto_sentiment',
                         variant='4o_enhanced')
    dumped = json.loads(fanned.model_dump_json())
    assert (dumped['variant_group'], dumped['variant']) == ('crypto_sentiment', '4o_enhanced')
    single = json.loads(RunMetadata(model='gpt-4o-mini').model_dump_json())
    # Absent = today's JSON — the keys are omitted entirely, not null (no schema bump).
    assert 'variant_group' not in single and 'variant' not in single
