"""Loads and provides the application configuration."""
import json
from pathlib import Path
from typing import Any, Dict, Optional

from finiexragengine.types.config_types.app_config_types import AppConfig

# Project root = two levels up from this file (finiexragengine/configuration/ -> repo root)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PROJECT_ROOT / 'configs' / 'app_config.json'
_USER_CONFIG_PATH = _PROJECT_ROOT / 'user_configs' / 'app_config.json'
_PIPELINES_DIR = _PROJECT_ROOT / 'configs' / 'pipelines'
_PROMPTS_DIR = _PROJECT_ROOT / 'prompts'


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge `override` onto `base` (nested dicts merge, scalars replace)."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class AppConfigManager:
    """Loads configs/app_config.json into a typed AppConfig.

    Hierarchical: the tracked defaults (`configs/app_config.json`) are overlaid with an
    optional, gitignored `user_configs/app_config.json` (deep-merged) — the place for
    operator-specific values and secrets (account credit, telegram token) that must not
    be committed. AppConfig(**merged) is the Pydantic validation gate: a malformed or
    incomplete config fails loudly here at construction, before the service boots.

    Globally available — instantiate and use directly: AppConfigManager().get_config().
    """

    def __init__(self, config_path: Optional[Path] = None,
                 user_config_path: Optional[Path] = None) -> None:
        base_path = config_path or _CONFIG_PATH
        user_path = user_config_path or _USER_CONFIG_PATH
        data = json.loads(base_path.read_text(encoding='utf-8'))
        # Overlay operator/secret overrides when present (gitignored, optional).
        if user_path.exists():
            data = _deep_merge(data, json.loads(user_path.read_text(encoding='utf-8')))
        self._config = AppConfig(**data)

    def get_config(self) -> AppConfig:
        return self._config

    def get_pipelines_dir(self) -> Path:
        return _PIPELINES_DIR

    def get_prompts_dir(self) -> Path:
        return _PROMPTS_DIR
