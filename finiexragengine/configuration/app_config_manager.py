"""Loads and provides the application configuration."""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from finiexragengine.configuration.config_merge import deep_merge
from finiexragengine.configuration.override_report import (
    OverrideEntry,
    collect_overrides,
    emit_override_report,
)
from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.config_types.app_config_types import AppConfig

# Project root = two levels up from this file (finiexragengine/configuration/ -> repo root)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PROJECT_ROOT / 'configs' / 'app_config.json'
_USER_CONFIG_PATH = _PROJECT_ROOT / 'user_configs' / 'app_config.json'
_PIPELINES_DIR = _PROJECT_ROOT / 'configs' / 'pipelines'
_USER_PIPELINES_DIR = _PROJECT_ROOT / 'user_configs' / 'pipelines'
_SOURCE_SETS_DIR = _PROJECT_ROOT / 'configs' / 'source_sets'
_USER_SOURCE_SETS_DIR = _PROJECT_ROOT / 'user_configs' / 'source_sets'
_PROMPTS_DIR = _PROJECT_ROOT / 'prompts'
_MIGRATIONS_DIR = _PROJECT_ROOT / 'migrations'


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
        self._paths: List[Path] = [base_path, user_path]
        base = json.loads(base_path.read_text(encoding='utf-8'))
        data = base
        # Overlay operator/secret overrides when present (gitignored, optional).
        user_data: Optional[Dict[str, Any]] = None
        if user_path.exists():
            user_data = json.loads(user_path.read_text(encoding='utf-8'))
            data = deep_merge(base, user_data)
        self._config = AppConfig(**data)
        # Override visibility (once per process, gated by logging.warn_on_override):
        # WHAT the user file changes, leaf by leaf — a forgotten override or a typo'd
        # key must never steer a run silently.
        #
        # Kept as well as emitted (2026-09-08): the boot line is capped at six leaves and never
        # prints a string value, which makes it a notice rather than an answer. The config view
        # publishes the full list, so the entries have to survive the constructor — the registries
        # already keep theirs, and this was the one layer that dropped them on the floor.
        self._override_entries: List[OverrideEntry] = []
        if user_data is not None:
            self._override_entries = collect_overrides(base, user_data,
                                                       self._config.model_dump())
            self._emit_overrides('user_configs/app_config.json', self._override_entries)

    def get_config(self) -> AppConfig:
        return self._config

    def override_entries(self) -> List[OverrideEntry]:
        """What `user_configs/app_config.json` moved, leaf by leaf — empty when there is no overlay.

        Named like `PipelineRegistry.override_entries()` / `SourceSetRegistry.override_entries()`
        on purpose: three layers, one question, one spelling.
        """
        return list(self._override_entries)

    def config_paths(self) -> List[str]:
        """The files the effective app config was merged from, tracked layer first.

        Reported by the config view so a reader can see that an overlay exists even when it changed
        nothing — an absent file and an inert one are different states.
        """
        labels: List[str] = []
        for path in self._paths:
            if not path.exists():
                continue
            # A test (or a second deployment layout) may pass a path outside the repo; naming the
            # file is still the useful half, and a raised ValueError here would be absurd.
            # `as_posix()`, not `str()`: the engine runs on Windows and the served value would
            # otherwise read `configs\app_config.json` while the pipeline and source-set views —
            # whose layers are declared constants — read `configs/pipelines/`. One document, two
            # separators, decided by which machine answered. Found on the live surface, 2026-09-08.
            labels.append(path.relative_to(_PROJECT_ROOT).as_posix()
                          if path.is_relative_to(_PROJECT_ROOT) else path.name)
        return labels

    def get_pipelines_dir(self) -> Path:
        return _PIPELINES_DIR

    def get_user_pipelines_dir(self) -> Path:
        """Gitignored per-pipeline overrides — deep-merged onto the tracked constellation."""
        return _USER_PIPELINES_DIR

    def get_source_sets_dir(self) -> Path:
        return _SOURCE_SETS_DIR

    def get_user_source_sets_dir(self) -> Path:
        """Gitignored per-source-set overrides — deep-merged onto the tracked set.

        Reachability is machine-specific (a feed behind a bot-wall answers one egress IP and
        refuses another), so switching a feed off belongs here, not in the tracked catalogue.
        """
        return _USER_SOURCE_SETS_DIR

    def build_pipeline_registry(self) -> PipelineRegistry:
        """The one way to load constellations: tracked dir + gitignored user overrides.

        Call sites must never assemble a PipelineRegistry themselves — four CLIs once
        did and silently dropped the override merge. Routing every consumer through
        this factory makes forgetting the user dir impossible, exactly like the
        app-config merge in __init__ (an omission is correct, not wrong).
        """
        registry = PipelineRegistry(self.get_pipelines_dir(), self.get_user_pipelines_dir())
        registry.load()
        for name, entries in registry.override_entries().items():
            self._emit_overrides(f'user_configs/pipelines/{name}', entries)
        return registry

    def build_source_set_registry(self) -> SourceSetRegistry:
        """The one way to load source-sets: tracked catalogue + per-machine overrides."""
        registry = SourceSetRegistry(self.get_source_sets_dir(),
                                     self.get_user_source_sets_dir())
        registry.load()
        for name, entries in registry.override_entries().items():
            self._emit_overrides(f'user_configs/source_sets/{name}', entries)
        return registry

    def _emit_overrides(self, file_label: str, entries: List[OverrideEntry]) -> None:
        """Gate + forward: the manager decides whether override visibility is on
        (`logging.warn_on_override`) — callers and registries never read the flag.
        The report unit's process-wide dedupe keeps it to once per startup."""
        if self._config.logging.warn_on_override:
            emit_override_report(file_label, entries)

    def get_prompts_dir(self) -> Path:
        return _PROMPTS_DIR

    def get_migrations_dir(self) -> Path:
        """Numbered SQL migrations (ISSUE_14) — the schema's source of truth, applied in order."""
        return _MIGRATIONS_DIR
