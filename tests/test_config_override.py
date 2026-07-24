"""AppConfigManager base <- user_configs override (ISSUE_23; groundwork for #27 secrets)."""
import json
import logging
from pathlib import Path

from finiexragengine.configuration import override_report
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.types.config_types.app_config_types import AppConfig


def _write(path, data) -> None:
    path.write_text(json.dumps(data), encoding='utf-8')


def test_defaults_mirror_tracked_json():
    # The convention "config defaults must mirror configs/app_config.json exactly",
    # checked mechanically: applying the tracked file must change nothing vs pure defaults.
    tracked = json.loads((Path(__file__).resolve().parents[1] / 'configs'
                          / 'app_config.json').read_text(encoding='utf-8'))
    assert AppConfig(**tracked).model_dump() == AppConfig().model_dump()


def test_user_override_deep_merges(tmp_path):
    base = tmp_path / 'app_config.json'
    user = tmp_path / 'user_app_config.json'
    _write(base, {
        'llm': {'timeout_seconds': 30, 'temperature': 0.1},
        'cost': {'account_credit_usd': 0.0, 'budget_usd': 0.0},
    })
    _write(user, {'cost': {'account_credit_usd': 50.0},
                  'llm': {'temperature': 0.5,
                          'allowed_models': ['gpt-4o-mini', 'ft:gpt-4o-mini:acme::x1']}})
    cfg = AppConfigManager(config_path=base, user_config_path=user).get_config()
    assert cfg.cost.account_credit_usd == 50.0   # user override wins
    assert cfg.cost.budget_usd == 0.0            # base kept (deep merge, not replace)
    assert cfg.llm.temperature == 0.5            # nested scalar overridden
    assert cfg.llm.timeout_seconds == 30         # sibling under llm kept
    # The governance allowlist is replaced wholesale by the override — the operator
    # takes full ownership locally (e.g. admitting a fine-tuned model).
    assert cfg.llm.allowed_models == ['gpt-4o-mini', 'ft:gpt-4o-mini:acme::x1']


def test_no_user_file_uses_base(tmp_path):
    base = tmp_path / 'app_config.json'
    _write(base, {'cost': {'account_credit_usd': 7.0}})
    cfg = AppConfigManager(config_path=base, user_config_path=tmp_path / 'nope.json').get_config()
    assert cfg.cost.account_credit_usd == 7.0


# --- factory wiring: the ONE way to build registries applies the overrides ---
# Four CLIs once constructed PipelineRegistry raw and silently dropped user_configs/;
# the factories make the omission impossible. These tests pin exactly that wiring.

def _manager(tmp_path, monkeypatch, tracked, user) -> AppConfigManager:
    # Point the canonical dirs at tmp fixtures — the factories read them via the getters.
    monkeypatch.setattr(AppConfigManager, 'get_pipelines_dir', lambda self: tracked)
    monkeypatch.setattr(AppConfigManager, 'get_user_pipelines_dir', lambda self: user)
    monkeypatch.setattr(AppConfigManager, 'get_source_sets_dir', lambda self: tracked)
    monkeypatch.setattr(AppConfigManager, 'get_user_source_sets_dir', lambda self: user)
    base = tmp_path / 'app_config.json'
    _write(base, {})
    return AppConfigManager(config_path=base, user_config_path=tmp_path / 'nope.json')


def test_pipeline_factory_applies_user_override(tmp_path, monkeypatch, caplog):
    override_report._REPORTED.clear()                      # per-process spam guard
    tracked = tmp_path / 'pipelines'
    user = tmp_path / 'user_pipelines'
    tracked.mkdir(), user.mkdir()
    _write(tracked / 'p.json', {
        'pipeline_id': 'p', 'outcome_type': 'sentiment_fear_greed', 'market': 'crypto',
        'symbols': [{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'},
                    {'key': 'ETHUSD', 'base': 'ETH', 'quote': 'USD'}],
        'llm': {'model': 'gpt-4o-mini'},
        'source_set': 's', 'retrieval': {'floor_distance': 0.70}})
    _write(user / 'p.json', {'symbols': [{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}],
                             'retrieval': {'floor_distance': 0.65}})

    with caplog.at_level(logging.WARNING):
        registry = _manager(tmp_path, monkeypatch, tracked, user).build_pipeline_registry()
    config = registry.get('p').get_config()
    assert config.symbol_keys() == ['BTCUSD']              # list replaced wholesale
    assert config.retrieval.floor_distance == 0.65         # nested scalar overridden
    assert registry.is_overridden('p')                     # divergence is visible
    # The startup one-liner says WHAT diverges (gated by warn_on_override).
    report = '\n'.join(r.getMessage() for r in caplog.records if '[OVERRIDE]' in r.getMessage())
    assert 'pipelines/p.json' in report
    assert 'floor_distance 0.7→0.65' in report and 'symbols 2→1' in report


def test_source_set_factory_applies_user_override(tmp_path, monkeypatch):
    tracked = tmp_path / 'source_sets'
    user = tmp_path / 'user_source_sets'
    tracked.mkdir(), user.mkdir()
    _write(tracked / 's.json', {
        'source_set_id': 's',
        'sources': [{'source_id': 'f1', 'type': 'rss', 'url': 'https://a.test/rss'},
                    {'source_id': 'f2', 'type': 'rss', 'url': 'https://b.test/rss'}]})
    _write(user / 's.json', {'sources': [{'source_id': 'f2', 'enabled': False}]})

    registry = _manager(tmp_path, monkeypatch, tracked, user).build_source_set_registry()
    sources = {s.source_id: s for s in registry.get('s').sources}
    assert sources['f1'].enabled is True                   # untouched sibling kept
    assert sources['f2'].enabled is False                  # patched by source_id
