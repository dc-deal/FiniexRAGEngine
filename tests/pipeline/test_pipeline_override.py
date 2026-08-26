"""Per-pipeline user_configs override (deep-merge) — dev/live variant without touching the base."""
import json

from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry

_BASE = {
    'pipeline_id': 'crypto_sentiment', 'outcome_type': 'sentiment_fear_greed',
    'market': 'crypto', 'symbols': [
        {'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'},
        {'key': 'ETHUSD', 'base': 'ETH', 'quote': 'USD'},
        {'key': 'SOLUSD', 'base': 'SOL', 'quote': 'USD'}],
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


def test_override_patches_symbols_by_key_inherits_the_rest(tmp_path):
    # ISSUE_70: symbols merge by `key` (not wholesale) — an override patches a listed symbol
    # (here BTCUSD's query) and leaves the unlisted ones (SOLUSD) + other config (source_set) intact.
    registry = _registry(tmp_path, {'symbols': [{'key': 'BTCUSD', 'query': 'Bitcoin BTC news'}]})
    ids = sorted(p.get_config().pipeline_id for p in registry.list_pipelines())
    assert ids == ['crypto_sentiment', 'crypto_sentiment_4o_enhanced']   # fan still expands
    config = registry.get('crypto_sentiment').get_config()
    assert config.symbol_keys() == ['BTCUSD', 'ETHUSD', 'SOLUSD']        # none dropped (merge-by-key)
    assert next(s for s in config.symbols if s.key == 'BTCUSD').query == 'Bitcoin BTC news'  # patched
    assert config.source_set == 'crypto_news'                            # inherited, not restated


def test_override_can_switch_llm_form_to_single_model(tmp_path):
    # Base is a 2-model fan; the override switches to one model — the guard drops the base
    # `models`, so the merge is a valid single-model pipeline (no model/models XOR conflict).
    registry = _registry(tmp_path, {'llm': {'model': 'gpt-4o-mini'}})
    ids = [p.get_config().pipeline_id for p in registry.list_pipelines()]
    assert ids == ['crypto_sentiment']                   # no fan -> one stream
    assert registry.get('crypto_sentiment').get_config().llm.model == 'gpt-4o-mini'


def test_no_override_file_leaves_the_base_untouched(tmp_path):
    registry = _registry(tmp_path)                       # no override written
    assert registry.get('crypto_sentiment').get_config().symbol_keys() == ['BTCUSD', 'ETHUSD', 'SOLUSD']


def test_override_disables_one_symbol_via_merge_by_key(tmp_path):
    # ISSUE_70: a one-line override flips SOLUSD's `enabled` — merged by `key`, so the other symbols
    # (and SOL's base/quote) are inherited, and the disabled symbol drops out of the active run set.
    registry = _registry(tmp_path, {'symbols': [{'key': 'SOLUSD', 'enabled': False}]})
    config = registry.get('crypto_sentiment').get_config()
    assert config.symbol_keys() == ['BTCUSD', 'ETHUSD']  # SOLUSD off; the rest still active
    # Still defined (base/quote inherited via the patch), just not active.
    sol = next(s for s in config.symbols if s.key == 'SOLUSD')
    assert (sol.base, sol.quote, sol.enabled) == ('SOL', 'USD', False)


def test_override_toggles_one_variant_enabled_via_merge_by_key(tmp_path):
    # A one-line override flips 4o's `enabled` — merged by sub_pipeline_id, so mini and 4o's
    # name/default are inherited (the value is not restated), and the disabled variant is dropped.
    registry = _registry(tmp_path, {'llm': {'models': [
        {'sub_pipeline_id': '4o_enhanced', 'enabled': False}]}})
    ids = sorted(p.get_config().pipeline_id for p in registry.list_pipelines())
    assert ids == ['crypto_sentiment']                   # 4o off; the default (mini) still runs
    assert registry.get('crypto_sentiment').get_config().llm.model == 'gpt-4o-mini'
