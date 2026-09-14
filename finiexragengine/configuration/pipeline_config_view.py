"""`GET /v1/configs/pipelines` — the constellations this process is running (2026-09-08)."""
from typing import Dict, List, Tuple

from finiexragengine.configuration.abstract_config_view import AbstractConfigView
from finiexragengine.configuration.override_report import OverrideEntry
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


class PipelineConfigView(AbstractConfigView):
    """Every constellation, keyed by `pipeline_id`.

    The values a remote reader most often needs are here and nowhere else on the API: the prompt
    version and retrieval floor an envelope only hints at, the deep tier's window, the breaking
    gates. `/v1/pipelines` answers a consumer's question (symbols, cadence); this answers the
    operator's.
    """

    NAME = 'pipelines'
    SUMMARY = 'constellations: symbols, prompt, model variants, retrieval and breaking thresholds'
    LAYERS: Tuple[str, ...] = ('configs/pipelines/', 'user_configs/pipelines/')

    def __init__(self, registry: PipelineRegistry) -> None:
        # The registry `create_app` built and the workers evaluate from (loaded through
        # `AppConfigManager.build_pipeline_registry`, so the overlay is already merged in).
        self._registry = registry

    def documents(self) -> Dict[str, PipelineConfig]:
        return {pipeline.get_config().pipeline_id: pipeline.get_config()
                for pipeline in self._registry.list_pipelines()}

    def override_entries(self) -> Dict[str, List[OverrideEntry]]:
        # The registry keys its entries by FILE name (`crypto_sentiment.json`); the documents are
        # keyed by `pipeline_id`. They agree today and are not required to, so the file stem is
        # what maps them — a fan-out variant has its own id and no file of its own.
        return {name.removesuffix('.json'): entries
                for name, entries in self._registry.override_entries().items()}
