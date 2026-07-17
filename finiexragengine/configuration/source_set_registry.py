"""Loads source-set JSONs and serves them by id (ISSUE_10)."""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from finiexragengine.configuration.config_merge import deep_merge
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.source_set_types import SourceSetConfig

logger = logging.getLogger(__name__)

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

    def load(self) -> None:
        """Load every source-set JSON in the source-sets directory."""
        for path in sorted(self._source_sets_dir.glob('*.json')):
            data = self._with_override(path)
            config = SourceSetConfig(**data)
            # Two files claiming one id would silently swap feeds under a pipeline.
            if config.source_set_id in self._source_sets:
                raise ConfigurationError(
                    f"duplicate source_set_id '{config.source_set_id}' "
                    f'(from {path.name}) — source-set ids must be unique')
            self._source_sets[config.source_set_id] = config

    def _with_override(self, path: Path) -> Dict[str, Any]:
        """Load a source-set, deep-merging a gitignored user override when one exists.

        Override only what differs — everything else is inherited from the tracked base. Because
        `sources` merges by `source_id`, an override states just the feed it changes (e.g. one
        `enabled: false` plus the reason in `comment`); the other feeds are kept untouched.
        """
        data = json.loads(path.read_text(encoding='utf-8'))
        if self._user_overrides_dir is None:
            return data
        override_path = self._user_overrides_dir / path.name
        if not override_path.exists():
            return data
        override = json.loads(override_path.read_text(encoding='utf-8'))
        merged = deep_merge(data, override, _OVERRIDE_LIST_KEYS)
        logger.info("source-set '%s' overridden from user_configs/source_sets/%s",
                    data.get('source_set_id', path.stem), path.name)
        return merged

    def list_sets(self) -> List[SourceSetConfig]:
        return list(self._source_sets.values())

    def get(self, source_set_id: str) -> SourceSetConfig:
        if source_set_id not in self._source_sets:
            raise ConfigurationError(
                f"unknown source_set '{source_set_id}' — expected one of "
                f'{sorted(self._source_sets)} (configs/source_sets/)')
        return self._source_sets[source_set_id]
