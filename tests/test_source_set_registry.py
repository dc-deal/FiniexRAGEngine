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


# --- enabled / comment + the per-machine override ------------------------------------

_TWO_FEEDS = {
    'source_set_id': 'crypto_news',
    'sources': [
        {'source_id': 'good', 'type': 'rss', 'url': 'https://example.test/a', 'weight': 1.0,
         'comment': 'high-trust source, no known problems'},
        {'source_id': 'walled', 'type': 'rss', 'url': 'https://example.test/b', 'weight': 0.8},
    ],
}


def test_sources_are_enabled_by_default_and_comment_is_optional(tmp_path):
    # The tracked catalogue stays the default: nothing switched off unless an override says so.
    source_set = _registry(tmp_path, _TWO_FEEDS).get('crypto_news')
    assert [s.enabled for s in source_set.sources] == [True, True]
    assert source_set.sources[0].comment == 'high-trust source, no known problems'
    assert source_set.sources[1].comment is None


def test_override_disables_one_feed_without_restating_the_array(tmp_path):
    # The point of patch-by-id: an override names ONLY the feed it changes. Reachability is a
    # per-machine fact, so the tracked set must stay untouched and the siblings must survive.
    base_dir, user_dir = tmp_path / 'configs', tmp_path / 'user'
    base_dir.mkdir()
    user_dir.mkdir()
    (base_dir / 'crypto_news.json').write_text(json.dumps(_TWO_FEEDS))
    (user_dir / 'crypto_news.json').write_text(json.dumps({
        'source_set_id': 'crypto_news',
        'sources': [{'source_id': 'walled', 'enabled': False,
                     'comment': 'Cf-Mitigated: challenge from this egress IP'}],
    }))
    registry = SourceSetRegistry(base_dir, user_dir)
    registry.load()
    sources = {s.source_id: s for s in registry.get('crypto_news').sources}

    assert sources['walled'].enabled is False                  # the override took
    assert sources['walled'].comment == 'Cf-Mitigated: challenge from this egress IP'
    assert sources['walled'].url == 'https://example.test/b'   # base fields survive the patch
    assert sources['good'].enabled is True                     # untouched sibling kept
    assert sources['good'].comment == 'high-trust source, no known problems'


def test_active_sources_excludes_the_disabled_ones(tmp_path):
    # `active_sources()` is the single definition of "what runs": the ingestor builds exactly this
    # list, and SourceReach takes its census over it. A disabled feed must be in neither envelope
    # number — counting it would report a contribution that never existed (8/8 for seven feeds).
    base_dir, user_dir = tmp_path / 'configs', tmp_path / 'user'
    base_dir.mkdir()
    user_dir.mkdir()
    (base_dir / 'crypto_news.json').write_text(json.dumps(_TWO_FEEDS))
    (user_dir / 'crypto_news.json').write_text(json.dumps({
        'source_set_id': 'crypto_news',
        'sources': [{'source_id': 'walled', 'enabled': False}],
    }))
    registry = SourceSetRegistry(base_dir, user_dir)
    registry.load()
    source_set = registry.get('crypto_news')

    assert len(source_set.sources) == 2                     # still declared (knowledge kept)
    assert [s.source_id for s in source_set.active_sources()] == ['good']   # but only one runs


@pytest.mark.parametrize('make_dir', [False, True],
                         ids=['override_dir_absent', 'override_dir_empty'])
def test_no_override_on_this_machine_is_a_no_op(tmp_path, make_dir):
    # Both are the everyday case and must behave identically: `user_configs/source_sets/` does
    # not exist at all on a fresh checkout (make_dir=False), or exists without a matching file.
    # Either way the tracked catalogue runs verbatim — an absent override must never raise.
    base_dir, user_dir = tmp_path / 'configs', tmp_path / 'user'
    base_dir.mkdir()
    if make_dir:
        user_dir.mkdir()
    (base_dir / 'crypto_news.json').write_text(json.dumps(_TWO_FEEDS))
    registry = SourceSetRegistry(base_dir, user_dir)
    registry.load()
    source_set = registry.get('crypto_news')
    assert [s.enabled for s in source_set.sources] == [True, True]
    assert len(source_set.active_sources()) == 2


def test_registry_without_an_override_dir_at_all(tmp_path):
    # The param defaults to None (e.g. a caller that never opts in) — also a plain no-op.
    (tmp_path / 'crypto_news.json').write_text(json.dumps(_TWO_FEEDS))
    registry = SourceSetRegistry(tmp_path)
    registry.load()
    assert len(registry.get('crypto_news').active_sources()) == 2
