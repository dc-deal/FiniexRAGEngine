"""The three configuration layers — public reference, private layer, machine overlay.

Built in temporary directories laid out like the real ones (`configs/`, `configs_private/`,
`user_configs/`), so the layer labels the boot report prints are the real ones too.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from finiexragengine.configuration import override_report
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.configuration.config_layers import group_entries_by_stem
from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.exceptions.ragengine_errors import ConfigurationError

_PUBLIC = {
    'pipeline_id': 'public_view', 'outcome_type': 'sentiment_fear_greed', 'market': 'crypto',
    'symbols': [{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD', 'query': 'Bitcoin'}],
    'source_set': 'news', 'llm': {'model': 'gpt-4o-mini'},
    'retrieval': {'floor_distance': 0.7},
}
_PRIVATE = {
    'pipeline_id': 'private_view', 'outcome_type': 'sentiment_fear_greed', 'market': 'crypto',
    'symbols': [{'key': 'ETHUSD', 'base': 'ETH', 'quote': 'USD', 'query': 'the environment'}],
    'source_set': 'news', 'prompt': {'name': 'private_family', 'version': '1'},
    'llm': {'models': [{'name': 'gpt-5-mini', 'sub_pipeline_id': 'mini', 'default': True},
                       {'name': 'gpt-5', 'sub_pipeline_id': 'gpt5'}]},
}
_SET = {
    'source_set_id': 'news', 'trigger': {'type': 'interval', 'interval_seconds': 15},
    'sources': [{'source_id': 'a', 'type': 'rss', 'url': 'https://a.test/rss', 'weight': 1.0},
                {'source_id': 'b', 'type': 'rss', 'url': 'https://b.test/rss', 'weight': 0.8}],
}


def _write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def _layers(tmp_path: Path, kind: str, *, tracked: Optional[Dict[str, Dict[str, Any]]] = None,
            private: Optional[Dict[str, Dict[str, Any]]] = None,
            user: Optional[Dict[str, Dict[str, Any]]] = None):
    """Write each layer's files; return the three directories of one kind."""
    dirs = {layer: tmp_path / layer / kind
            for layer in ('configs', 'configs_private', 'user_configs')}
    dirs['configs'].mkdir(parents=True, exist_ok=True)
    for layer, files in (('configs', tracked), ('configs_private', private),
                         ('user_configs', user)):
        for name, data in (files or {}).items():
            _write(dirs[layer] / name, data)
    return dirs['configs'], dirs['configs_private'], dirs['user_configs']


def _pipelines(tmp_path: Path, **files) -> PipelineRegistry:
    tracked, private, user = _layers(tmp_path, 'pipelines', **files)
    registry = PipelineRegistry(tracked, user, private)
    registry.load()
    return registry


# --- pipelines ------------------------------------------------------------------------------

def test_a_complete_private_pipeline_loads_and_expands_its_variants(tmp_path):
    registry = _pipelines(tmp_path, tracked={'public_view.json': _PUBLIC},
                          private={'private_view.json': _PRIVATE})
    ids = sorted(p.get_config().pipeline_id for p in registry.list_pipelines())
    assert ids == ['private_view', 'private_view_gpt5', 'public_view']
    assert registry.is_private('private_view') and registry.is_private('private_view_gpt5')
    assert not registry.is_private('public_view')
    assert not registry.is_overridden('private_view')       # a definition, not an overlay


def test_a_private_file_named_like_a_tracked_one_is_an_overlay_onto_it(tmp_path):
    registry = _pipelines(tmp_path, tracked={'public_view.json': _PUBLIC},
                          private={'public_view.json': {'retrieval': {'floor_distance': 0.6}}})
    config = registry.get('public_view').get_config()
    assert config.retrieval.floor_distance == 0.6
    assert config.symbols[0].query == 'Bitcoin'               # everything else inherited
    assert registry.is_overridden('public_view') and not registry.is_private('public_view')
    assert list(registry.override_entries()) == ['configs_private/pipelines/public_view.json']


def test_the_machine_overlay_applies_last_on_top_of_both_layers(tmp_path):
    registry = _pipelines(
        tmp_path, tracked={'public_view.json': _PUBLIC},
        private={'public_view.json': {'retrieval': {'floor_distance': 0.6}},
                 'private_view.json': _PRIVATE},
        user={'public_view.json': {'retrieval': {'floor_distance': 0.5}},
              'private_view.json': {'llm': {'models': [{'sub_pipeline_id': 'gpt5',
                                                        'enabled': False}]}}})
    assert registry.get('public_view').get_config().retrieval.floor_distance == 0.5
    ids = sorted(p.get_config().pipeline_id for p in registry.list_pipelines())
    assert ids == ['private_view', 'public_view']             # the shadow switched off here
    # Each layer's change is recorded against what it was merged onto, under its own label.
    entries = registry.override_entries()
    assert sorted(entries) == ['configs_private/pipelines/public_view.json',
                               'user_configs/pipelines/private_view.json',
                               'user_configs/pipelines/public_view.json']
    private_step = entries['configs_private/pipelines/public_view.json'][0]
    user_step = entries['user_configs/pipelines/public_view.json'][0]
    assert (private_step.base_value, private_step.override_value) == (0.7, 0.6)
    assert (user_step.base_value, user_step.override_value) == (0.6, 0.5)


def test_each_layer_may_switch_the_llm_form_on_its_own(tmp_path):
    # The XOR guard runs per overlay: the private layer narrows to one model, the machine widens
    # back to variants — both are valid, and neither leaves "both forms present" behind.
    registry = _pipelines(
        tmp_path, private={'private_view.json': _PRIVATE},
        user={'private_view.json': {'llm': {'model': 'gpt-5-mini'}}})
    config = registry.get('private_view').get_config()
    assert config.llm.model == 'gpt-5-mini' and config.llm.models is None


def test_a_private_definition_reusing_a_public_id_is_refused(tmp_path):
    clash = dict(_PRIVATE, pipeline_id='public_view')
    with pytest.raises(ConfigurationError, match="duplicate pipeline_id 'public_view'"):
        _pipelines(tmp_path, tracked={'public_view.json': _PUBLIC},
                   private={'other_name.json': clash})


def test_an_invalid_private_definition_says_it_is_private(tmp_path):
    broken = dict(_PRIVATE, symbols=[{'key': 'ETHUSD', 'base': 'ETH', 'quote': 'EUR'}])
    with pytest.raises(ConfigurationError, match='private pipeline config private_view.json'):
        _pipelines(tmp_path, private={'private_view.json': broken})


def test_no_private_layer_is_exactly_the_two_layer_behaviour(tmp_path):
    tracked, _private, user = _layers(tmp_path, 'pipelines',
                                      tracked={'public_view.json': _PUBLIC})
    for private_dir in (None, tmp_path / 'missing'):
        registry = PipelineRegistry(tracked, user, private_dir)
        registry.load()
        assert [p.get_config().pipeline_id for p in registry.list_pipelines()] == ['public_view']


# --- source sets ----------------------------------------------------------------------------

def test_source_sets_take_the_same_three_layers(tmp_path):
    tracked, private, user = _layers(
        tmp_path, 'source_sets', tracked={'news.json': _SET},
        private={'news.json': {'sources': [{'source_id': 'b', 'weight': 0.5}]},
                 'private_news.json': dict(_SET, source_set_id='private_news')},
        user={'news.json': {'sources': [{'source_id': 'a', 'enabled': False}]}})
    registry = SourceSetRegistry(tracked, user, private)
    registry.load()
    news = registry.get('news')
    assert [(s.source_id, s.enabled, s.weight) for s in news.sources] == [
        ('a', False, 1.0), ('b', True, 0.5)]
    assert registry.is_private('private_news') and not registry.is_private('news')


def test_an_invalid_source_set_is_a_configuration_refusal(tmp_path):
    tracked, private, user = _layers(tmp_path, 'source_sets',
                                     private={'bad.json': {'source_set_id': 'bad'}})
    registry = SourceSetRegistry(tracked, user, private)
    with pytest.raises(ConfigurationError, match='private source set bad.json'):
        registry.load()


# --- the factory and the views --------------------------------------------------------------

def test_the_factory_loads_the_private_layer_and_reports_it_under_its_own_label(
        tmp_path, monkeypatch, caplog):
    # The suite switches the private layer off by default (conftest); this test switches it on.
    tracked, private, user = _layers(
        tmp_path, 'pipelines', tracked={'public_view.json': _PUBLIC},
        private={'public_view.json': {'retrieval': {'floor_distance': 0.6}},
                 'private_view.json': _PRIVATE})
    monkeypatch.setattr(AppConfigManager, 'get_pipelines_dir', lambda self: tracked)
    monkeypatch.setattr(AppConfigManager, 'get_private_pipelines_dir', lambda self: private)
    monkeypatch.setattr(AppConfigManager, 'get_user_pipelines_dir', lambda self: user)
    base = tmp_path / 'app_config.json'
    _write(base, {})
    override_report._REPORTED.clear()
    with caplog.at_level(logging.INFO):
        registry = AppConfigManager(config_path=base,
                                    user_config_path=tmp_path / 'absent.json'
                                    ).build_pipeline_registry()
    assert registry.is_private('private_view')
    assert 'configs_private/pipelines/public_view.json' in caplog.text
    assert 'floor_distance 0.7→0.6' in caplog.text


def test_a_view_meets_both_layers_of_one_document_under_its_stem(tmp_path):
    registry = _pipelines(
        tmp_path, tracked={'public_view.json': _PUBLIC},
        private={'public_view.json': {'retrieval': {'floor_distance': 0.6}}},
        user={'public_view.json': {'retrieval': {'top_k': 8}}})
    grouped = group_entries_by_stem(registry.override_entries())
    assert list(grouped) == ['public_view']
    assert [entry.path for entry in grouped['public_view']] == [
        'retrieval.floor_distance', 'retrieval.top_k']        # private first, then the machine
