"""Loads and provides the application configuration."""
import json
from pathlib import Path

from finiexragengine.types.config_types.app_config_types import AppConfig

# Project root = two levels up from this file (finiexragengine/configuration/ -> repo root)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PROJECT_ROOT / 'configs' / 'app_config.json'
_PIPELINES_DIR = _PROJECT_ROOT / 'configs' / 'pipelines'


class AppConfigManager:
    """Loads configs/app_config.json into a typed AppConfig.

    Globally available — instantiate and use directly:
    AppConfigManager().get_config().
    """

    def __init__(self) -> None:
        data = json.loads(_CONFIG_PATH.read_text(encoding='utf-8'))
        self._config = AppConfig(**data)

    def get_config(self) -> AppConfig:
        return self._config

    def get_pipelines_dir(self) -> Path:
        return _PIPELINES_DIR
