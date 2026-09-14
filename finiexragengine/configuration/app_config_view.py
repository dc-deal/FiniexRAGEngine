"""`GET /v1/configs/app` — the engine-wide settings this process is running (2026-09-08)."""
from typing import Dict, List

from finiexragengine.configuration.abstract_config_view import AbstractConfigView
from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.configuration.override_report import OverrideEntry
from finiexragengine.types.config_types.app_config_types import AppConfig

# The single document's key. A name rather than an index, so `?id=` behaves the same here as it
# does for the two collection views.
_DOCUMENT = 'app'


class AppConfigView(AbstractConfigView):
    """The app config, minus the three leaves that are credentials.

    This is the layer that actually holds secrets — the bearer tokens and the Telegram bot's
    identity live in `user_configs/app_config.json` and nowhere else — so it is the reason the base
    class owns the projection instead of trusting a router to call one.
    """

    NAME = 'app'
    SUMMARY = 'engine-wide settings: models, budget, logging, health policy, report defaults'

    def __init__(self, manager: AppConfigManager) -> None:
        # The manager this process booted with, never a fresh read: a file edited after startup
        # must not make this surface disagree with the engine that is running.
        self._manager = manager

    def layers(self) -> List[str]:
        # Resolved per process rather than declared: a `user_configs/app_config.json` that does not
        # exist must not be listed as if it did — an absent overlay and an inert one are different
        # states, and only one of them is worth investigating.
        return self._manager.config_paths()

    def documents(self) -> Dict[str, AppConfig]:
        return {_DOCUMENT: self._manager.get_config()}

    def override_entries(self) -> Dict[str, List[OverrideEntry]]:
        entries = self._manager.override_entries()
        return {_DOCUMENT: entries} if entries else {}
