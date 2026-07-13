"""Per-pipeline user_configs override (deep-merge) — dev/live variant without touching the base."""
import json

from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry

_BASE = {
    'pipeline_id': 'crypto_sentiment', 'outcome_type': 'sentiment_fear_greed',
    'market': 'crypto', 'symbols': ['BTCUSD', 'ETHUSD', 'SOLUSD'],
    'source_set': 'crypto_news',
    'llm': {'models': [
        {'name': 'gpt-4o-mini', 'sub_pipeline_id': 'mini', 'default': True},
        {'name': 'gpt-4o', 'sub_pipeline_id': '4o_enhanced'}]},
}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _registry(tmp_path, override=None):
    _write(tmp_path / 'pipes' / 'crypto_sentiment.json', _BASE)
    if override is not None:
        _write(tmp_path / 'user' / 'crypto_sentiment.json', override)
    registry = PipelineRegistry(tmp_path / 'pipes', tmp_path / 'user')
    registry.load()
    return registry


def test_override_replaces_symbols_inherits_the_rest(tmp_path):
    registry = _registry(tmp_path, {'symbols': ['BTCUSD', 'ETHUSD']})
    # Both variants still expand (models inherited from the base), each with the new symbols.
    ids = sorted(p.get_config().pipeline_id for p in registry.list_pipelines())
    assert ids == ['crypto_sentiment', 'crypto_sentiment_4o_enhanced']
    config = registry.get('crypto_sentiment').get_config()
    assert config.symbols == ['BTCUSD', 'ETHUSD']        # replaced wholesale
    assert config.source_set == 'crypto_news'            # inherited, not restated


def test_override_can_switch_llm_form_to_single_model(tmp_path):
    # Base is a 2-model fan; the override switches to one model — the guard drops the base
    # `models`, so the merge is a valid single-model pipeline (no model/models XOR conflict).
    registry = _registry(tmp_path, {'llm': {'model': 'gpt-4o-mini'}})
    ids = [p.get_config().pipeline_id for p in registry.list_pipelines()]
    assert ids == ['crypto_sentiment']                   # no fan -> one stream
    assert registry.get('crypto_sentiment').get_config().llm.model == 'gpt-4o-mini'


def test_no_override_file_leaves_the_base_untouched(tmp_path):
    registry = _registry(tmp_path)                       # no override written
    assert registry.get('crypto_sentiment').get_config().symbols == ['BTCUSD', 'ETHUSD', 'SOLUSD']
