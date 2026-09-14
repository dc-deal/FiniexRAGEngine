"""`GET /v1/configs/source_sets` — the feed catalogue this process is polling (2026-09-08)."""
from typing import Dict, List, Tuple

from finiexragengine.configuration.abstract_config_view import AbstractConfigView
from finiexragengine.configuration.override_report import OverrideEntry
from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.types.config_types.source_set_types import SourceSetConfig


class SourceSetConfigView(AbstractConfigView):
    """Every source set, keyed by `source_set_id`.

    The layer the overlay moves most: reachability is machine-specific, so a feed switched off in
    `user_configs/source_sets/` is invisible in the tracked catalogue — and that single boolean
    changes what every detection threshold in the same file means. This view is what makes the
    2026-09-10 `high_cluster_size` reading possible without a session on the host.
    """

    NAME = 'source_sets'
    SUMMARY = 'feed catalogue: which sources are enabled, their weights, detection thresholds'
    LAYERS: Tuple[str, ...] = ('configs/source_sets/', 'user_configs/source_sets/')

    def __init__(self, registry: SourceSetRegistry) -> None:
        # The registry the ingest workers themselves poll from — `PipelineAssembler` holds it, so a
        # document served here is the catalogue a pass actually used.
        self._registry = registry

    def documents(self) -> Dict[str, SourceSetConfig]:
        return {source_set.source_set_id: source_set for source_set in self._registry.list_sets()}

    def override_entries(self) -> Dict[str, List[OverrideEntry]]:
        return {name.removesuffix('.json'): entries
                for name, entries in self._registry.override_entries().items()}
