"""Loads source-set JSONs and serves them by id (ISSUE_10)."""
import json
from pathlib import Path
from typing import Dict, List

from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.source_set_types import SourceSetConfig


class SourceSetRegistry:
    """Discovers and holds the configured source-sets.

    Each *.json in configs/source_sets/ is one shared feed group, referenced by
    constellations via `source_set: "<id>"`. Loading is the Pydantic validation gate
    (malformed sets fail at boot); an unresolved reference fails at assembly — both
    fail fast, before any spend.
    """

    def __init__(self, source_sets_dir: Path) -> None:
        self._source_sets_dir = source_sets_dir
        self._source_sets: Dict[str, SourceSetConfig] = {}

    def load(self) -> None:
        """Load every source-set JSON in the source-sets directory."""
        for path in sorted(self._source_sets_dir.glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            config = SourceSetConfig(**data)
            # Two files claiming one id would silently swap feeds under a pipeline.
            if config.source_set_id in self._source_sets:
                raise ConfigurationError(
                    f"duplicate source_set_id '{config.source_set_id}' "
                    f'(from {path.name}) — source-set ids must be unique')
            self._source_sets[config.source_set_id] = config

    def list_sets(self) -> List[SourceSetConfig]:
        return list(self._source_sets.values())

    def get(self, source_set_id: str) -> SourceSetConfig:
        if source_set_id not in self._source_sets:
            raise ConfigurationError(
                f"unknown source_set '{source_set_id}' — expected one of "
                f'{sorted(self._source_sets)} (configs/source_sets/)')
        return self._source_sets[source_set_id]
