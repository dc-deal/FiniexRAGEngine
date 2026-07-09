"""AppConfigManager base <- user_configs override (ISSUE_23; groundwork for #27 secrets)."""
import json

from finiexragengine.configuration.app_config_manager import AppConfigManager


def _write(path, data) -> None:
    path.write_text(json.dumps(data), encoding='utf-8')


def test_user_override_deep_merges(tmp_path):
    base = tmp_path / 'app_config.json'
    user = tmp_path / 'user_app_config.json'
    _write(base, {
        'llm': {'model': 'gpt-4o-mini', 'temperature': 0.1},
        'cost': {'account_credit_usd': 0.0, 'budget_usd': 0.0},
    })
    _write(user, {'cost': {'account_credit_usd': 50.0}, 'llm': {'temperature': 0.5}})
    cfg = AppConfigManager(config_path=base, user_config_path=user).get_config()
    assert cfg.cost.account_credit_usd == 50.0   # user override wins
    assert cfg.cost.budget_usd == 0.0            # base kept (deep merge, not replace)
    assert cfg.llm.temperature == 0.5            # nested scalar overridden
    assert cfg.llm.model == 'gpt-4o-mini'        # sibling under llm kept


def test_no_user_file_uses_base(tmp_path):
    base = tmp_path / 'app_config.json'
    _write(base, {'cost': {'account_credit_usd': 7.0}})
    cfg = AppConfigManager(config_path=base, user_config_path=tmp_path / 'nope.json').get_config()
    assert cfg.cost.account_credit_usd == 7.0
