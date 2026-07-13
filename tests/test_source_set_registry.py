"""Source-set registry + schema (ISSUE_10) — no DB, no API."""
import json

import pytest

from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.exceptions.ragengine_errors import ConfigurationError

_SET = {
    'source_set_id': 'crypto_news',
    'sources': [{'source_id': 'cryptonews', 'type': 'rss',
                 'url': 'https://example.test/rss', 'weight': 0.8}],
}


def _registry(tmp_path, *sets) -> SourceSetRegistry:
    for i, data in enumerate(sets):
        (tmp_path / f'set_{i}.json').write_text(json.dumps(data))
    registry = SourceSetRegistry(tmp_path)
    registry.load()
    return registry


def test_loads_and_serves_by_id(tmp_path):
    registry = _registry(tmp_path, _SET)
    source_set = registry.get('crypto_news')
    assert source_set.sources[0].source_id == 'cryptonews'
    # Ingest cadence defaults faster than eval (RSS windows slide — 5 min).
    assert source_set.trigger.interval_seconds == 300


def test_unknown_reference_fails_fast(tmp_path):
    registry = _registry(tmp_path, _SET)
    with pytest.raises(ConfigurationError, match="unknown source_set 'nope'"):
        registry.get('nope')


def test_duplicate_ids_refuse_to_load(tmp_path):
    (tmp_path / 'a.json').write_text(json.dumps(_SET))
    (tmp_path / 'b.json').write_text(json.dumps(_SET))
    registry = SourceSetRegistry(tmp_path)
    with pytest.raises(ConfigurationError, match='duplicate source_set_id'):
        registry.load()


def test_tracked_source_sets_load():
    # The real configs/source_sets/ directory stays loadable (the migration gate).
    from finiexragengine.configuration.app_config_manager import AppConfigManager
    registry = SourceSetRegistry(AppConfigManager().get_source_sets_dir())
    registry.load()
    ids = {s.source_set_id for s in registry.list_sets()}
    assert {'crypto_news', 'forex_news'} <= ids
