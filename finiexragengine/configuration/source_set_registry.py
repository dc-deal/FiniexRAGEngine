"""Loads source-set JSONs and serves them by id (ISSUE_10)."""
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import ValidationError

from finiexragengine.configuration.config_layers import load_layered_documents
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
                 user_overrides_dir: Optional[Path] = None,
                 private_dir: Optional[Path] = None) -> None:
        self._source_sets_dir = source_sets_dir
        # Optional gitignored per-set overrides (deep-merged onto the tracked set, same pattern
        # as the constellation override) — the place to switch a feed off on *this* machine
        # without touching the committed catalogue. None = no overrides.
        self._user_overrides_dir = user_overrides_dir
        # Optional private layer (`configs_private/source_sets/`): our own feed groups and our
        # patches to the public catalogue, versioned in a private repository. None = no layer.
        self._private_dir = private_dir
        self._source_sets: Dict[str, SourceSetConfig] = {}
        self._private: set = set()
        # Per applied overlay, keyed by its layer label (`user_configs/source_sets/x.json`): the
        # touched leaves (old → new), for the startup override report — collected at load,
        # emitted by the AppConfigManager factory (gated there).
        self._override_entries: Dict[str, List[OverrideEntry]] = {}

    def load(self) -> None:
        """Load every source-set the three layers define (`configuration/config_layers.py`)."""
        documents = load_layered_documents(self._source_sets_dir, self._private_dir,
                                           self._user_overrides_dir, _OVERRIDE_LIST_KEYS)
        for document in documents:
            try:
                config = SourceSetConfig(**document.data)
            except ValidationError as exc:
                # A configuration refusal (exit 2, no restart loop), exactly as for a pipeline.
                origin = (' (merged with ' + ', '.join(o.label for o in document.overlays) + ')'
                          if document.overlays else '')
                kind = 'private source set' if document.private else 'source set'
                raise ConfigurationError(
                    f'{kind} {document.name}{origin} is invalid:\n{exc}') from exc
            validated = config.model_dump()
            for overlay in document.overlays:
                # Collected against the VALIDATED config: a key Pydantic dropped is a
                # typo candidate; a key with a schema default reads as '(added)'.
                self._override_entries[overlay.label] = collect_overrides(
                    overlay.base, overlay.overlay, validated, _OVERRIDE_LIST_KEYS)
            # Two files claiming one id would silently swap feeds under a pipeline — including a
            # private set reusing a public id.
            if config.source_set_id in self._source_sets:
                raise ConfigurationError(
                    f"duplicate source_set_id '{config.source_set_id}' "
                    f'(from {document.name}) — source-set ids must be unique')
            self._source_sets[config.source_set_id] = config
            if document.private:
                self._private.add(config.source_set_id)

    def is_private(self, source_set_id: str) -> bool:
        """Whether this set is defined by the private layer (not part of the public catalogue)."""
        return source_set_id in self._private

    def override_entries(self) -> Dict[str, List[OverrideEntry]]:
        """Touched leaves per applied overlay, keyed by its layer label (empty when none)."""
        return self._override_entries

    def list_sets(self) -> List[SourceSetConfig]:
        return list(self._source_sets.values())

    def excluded_source_ids(self, reader_set_id: str) -> List[str]:
        """Sources a pipeline on `reader_set_id` must not retrieve: the declared sets of others.

        A `declared`-scope set is read only by the pipelines whose `source_set` it is. Every other
        pipeline excludes the active sources of such sets — minus any source also active in a shared
        set, whose articles are everyone's by that set's own terms. Active sources only: a declared
        but disabled feed ingests nothing, so it has nothing to hide. Sorted, so the list is stable
        across boots and diffable in a log.
        """
        hidden = {source.source_id for config in self._source_sets.values()
                  if config.scope == 'declared' and config.source_set_id != reader_set_id
                  for source in config.active_sources()}
        shared = {source.source_id for config in self._source_sets.values()
                  if config.scope is None for source in config.active_sources()}
        return sorted(hidden - shared)

    def set_section_source_ids(self, section_set_id: str, reader_set_id: str) -> List[str]:
        """The feeds a pipeline on `reader_set_id` may fill its set section from — or a refusal.

        Three configurations could only ever produce an empty section, and each is refused here
        rather than discovered in a funnel weeks later: an unknown set; a `declared` set that is not
        the reader's own, which the scope rule hides from it in every window; and a set with no
        active feed. Sorted, like `excluded_source_ids`, so the list is stable across boots.
        """
        section_set = self.get(section_set_id)
        if section_set.scope == 'declared' and section_set_id != reader_set_id:
            raise ConfigurationError(
                f"retrieval.set_section.source_set '{section_set_id}' is declared-scope and not this "
                f"pipeline's source_set ('{reader_set_id}') — the scope rule hides it from this "
                'pipeline, so the section could only ever be empty')
        hidden = set(self.excluded_source_ids(reader_set_id))
        ids = sorted(source.source_id for source in section_set.active_sources()
                     if source.source_id not in hidden)
        if not ids:
            raise ConfigurationError(
                f"retrieval.set_section.source_set '{section_set_id}' has no active feed this "
                'pipeline may read — the section could only ever be empty')
        return ids

    def get(self, source_set_id: str) -> SourceSetConfig:
        if source_set_id not in self._source_sets:
            raise ConfigurationError(
                f"unknown source_set '{source_set_id}' — expected one of "
                f'{sorted(self._source_sets)} (configs/source_sets/, configs_private/source_sets/)')
        return self._source_sets[source_set_id]
