"""Loads source-set JSONs and serves them by id (ISSUE_10)."""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from finiexragengine.configuration.config_merge import deep_merge
from finiexragengine.configuration.override_report import OverrideEntry, collect_overrides
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.source_set_types import SourceSetConfig

# `sources` is a list of objects with a stable id, so an override patches a single feed *by id*
# instead of restating the whole array — flipping one `enabled` cannot silently drop the others.
_OVERRIDE_LIST_KEYS = {'sources': 'source_id'}


class SourceSetRegistry:
    """Discovers and holds the configured source-sets.

    Each *.json in configs/source_sets/ is one shared feed group, referenced by
    constellations via `source_set: "<id>"`. Loading is the Pydantic validation gate
    (malformed sets fail at boot); an unresolved reference fails at assembly — both
    fail fast, before any spend.
    """

    def __init__(self, source_sets_dir: Path,
                 user_overrides_dir: Optional[Path] = None) -> None:
        self._source_sets_dir = source_sets_dir
        # Optional gitignored per-set overrides (deep-merged onto the tracked set, same pattern
        # as the constellation override) — the place to switch a feed off on *this* machine
        # without touching the committed catalogue. None = no overrides.
        self._user_overrides_dir = user_overrides_dir
        self._source_sets: Dict[str, SourceSetConfig] = {}
        # Per overridden file: the touched leaves (old → new), for the startup override
        # report — collected at load, emitted by the AppConfigManager factory (gated there).
        self._override_entries: Dict[str, List[OverrideEntry]] = {}

    def load(self) -> None:
        """Load every source-set JSON in the source-sets directory."""
        for path in sorted(self._source_sets_dir.glob('*.json')):
            data, base, override = self._with_override(path)
            config = SourceSetConfig(**data)
            if override is not None:
                # Collected against the VALIDATED config: a key Pydantic dropped is a
                # typo candidate; a key with a schema default reads as '(added)'.
                self._override_entries[path.name] = collect_overrides(
                    base, override, config.model_dump(), _OVERRIDE_LIST_KEYS)
            # Two files claiming one id would silently swap feeds under a pipeline.
            if config.source_set_id in self._source_sets:
                raise ConfigurationError(
                    f"duplicate source_set_id '{config.source_set_id}' "
                    f'(from {path.name}) — source-set ids must be unique')
            self._source_sets[config.source_set_id] = config

    def _with_override(self, path: Path) -> Tuple[Dict[str, Any], Dict[str, Any],
                                                  Optional[Dict[str, Any]]]:
        """Load a source-set, deep-merging a gitignored user override when one exists.

        Override only what differs — everything else is inherited from the tracked base. Because
        `sources` merges by `source_id`, an override states just the feed it changes (e.g. one
        `enabled: false` plus the reason in `comment`); the other feeds are kept untouched.

        Returns:
            (merged, base, override) — base and override stay raw so the override report
            can render `old → new` per touched leaf; override is None when none exists.
        """
        data = json.loads(path.read_text(encoding='utf-8'))
        if self._user_overrides_dir is None:
            return data, data, None
        override_path = self._user_overrides_dir / path.name
        if not override_path.exists():
            return data, data, None
        override = json.loads(override_path.read_text(encoding='utf-8'))
        return deep_merge(data, override, _OVERRIDE_LIST_KEYS), data, override

    def override_entries(self) -> Dict[str, List[OverrideEntry]]:
        """Touched leaves per overridden source-set file (empty when none)."""
        return self._override_entries

    def list_sets(self) -> List[SourceSetConfig]:
        return list(self._source_sets.values())

    def get(self, source_set_id: str) -> SourceSetConfig:
        if source_set_id not in self._source_sets:
            raise ConfigurationError(
                f"unknown source_set '{source_set_id}' — expected one of "
                f'{sorted(self._source_sets)} (configs/source_sets/)')
        return self._source_sets[source_set_id]
