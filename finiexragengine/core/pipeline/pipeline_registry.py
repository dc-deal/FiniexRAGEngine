"""Loads constellation JSONs into Pipeline objects and serves them by id."""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from finiexragengine.configuration.config_layers import load_layered_documents
from finiexragengine.configuration.override_report import OverrideEntry, collect_overrides
from finiexragengine.core.pipeline.pipeline import Pipeline
from finiexragengine.exceptions.ragengine_errors import (
    ConfigurationError,
    PipelineNotFoundError,
)
from finiexragengine.types.config_types.pipeline_config_types import (
    PipelineConfig,
    PipelineLlmConfig,
)

logger = logging.getLogger(__name__)

# Lists whose items a user override merges *by id* (patch one item, keep the rest) rather than
# replacing wholesale — so an override can flip a single variant's `enabled`, a source's weight,
# or one symbol's `enabled` (ISSUE_70) without restating the whole array. Plain lists (keywords)
# still replace.
_OVERRIDE_LIST_KEYS = {'models': 'sub_pipeline_id', 'sources': 'source_id', 'symbols': 'key'}


def _keep_one_llm_form(merged: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Guard the llm XOR after each overlay: the form an overlay picks wins over the layer below.

    If an overlay sets `llm.model` the merged `models` list is dropped, and vice versa — switching
    forms via an overlay must never produce an invalid "both present". Run per layer, so a private
    overlay and a machine overlay can each switch forms on their own.
    """
    overlay_llm = overlay.get('llm', {})
    merged_llm = merged.get('llm')
    if isinstance(merged_llm, dict) and isinstance(overlay_llm, dict):
        if 'model' in overlay_llm and 'models' not in overlay_llm:
            merged_llm.pop('models', None)
        elif 'models' in overlay_llm and 'model' not in overlay_llm:
            merged_llm.pop('model', None)
    return merged


def expand_variants(config: PipelineConfig) -> List[PipelineConfig]:
    """Fan one constellation into its logical pipelines (ISSUE_42) — format A.

    Single-model configs pass through untouched. A `llm.models` constellation becomes
    one logical pipeline per variant: identical sources/symbols/retrieval/prompt, only
    the model differs. The `default` variant keeps the bare `pipeline_id` (archived
    series continue seamlessly); the others get `<pipeline_id>_<sub_pipeline_id>`.
    Every variant carries the grouping hints (`variant_group` = the default stream's
    id, `variant` = its own sub id) — stamped into the envelope by the runner.
    """
    if config.llm.models is None:
        return [config]
    expanded = []
    for variant in config.llm.models:
        if not variant.enabled:
            continue                     # defined but toggled off — no stream, no cost
        stream_id = (config.pipeline_id if variant.default
                     else f'{config.pipeline_id}_{variant.sub_pipeline_id}')
        # Each logical pipeline is a plain single-model config downstream — assembler,
        # runner, reports and the model check need zero fan-out awareness.
        expanded.append(config.model_copy(update={
            'pipeline_id': stream_id,
            'llm': PipelineLlmConfig(model=variant.name),
            'variant_group': config.pipeline_id,
            'variant': variant.sub_pipeline_id,
        }))
    return expanded


class PipelineRegistry:
    """Discovers and holds the configured pipelines.

    Each *.json in the pipelines directory is one constellation -> one Pipeline —
    or N logical pipelines when it declares model variants (ISSUE_42). Two further layers may
    contribute (`configuration/config_layers.py`): the private layer patches a tracked
    constellation or defines a complete private one, and the machine overlay patches either.
    """

    def __init__(self, pipelines_dir: Path,
                 user_overrides_dir: Optional[Path] = None,
                 private_dir: Optional[Path] = None) -> None:
        self._pipelines_dir = pipelines_dir
        # Optional gitignored per-pipeline overrides (deep-merged onto the tracked config, same
        # pattern as app_config) — the place for a dev/live variant (fewer symbols, other models)
        # without touching the committed constellation. None = no overrides.
        self._user_overrides_dir = user_overrides_dir
        # Optional private layer (`configs_private/pipelines/`): our own constellations and our
        # patches to the public reference, versioned in a private repository. None = no layer.
        self._private_dir = private_dir
        self._pipelines: Dict[str, Pipeline] = {}
        # Stream ids whose constellation carried an overlay from either layer (all variants of an
        # overlaid file) — surfaced e.g. in the cost report so a diverging config is visible.
        self._overridden: set = set()
        # Stream ids whose complete definition lives in the private layer (all their variants).
        self._private: set = set()
        # Per applied overlay, keyed by its layer label (`user_configs/pipelines/x.json`): the
        # touched leaves (old → new), for the startup override report — collected at load,
        # emitted by the AppConfigManager factory (gated there).
        self._override_entries: Dict[str, List[OverrideEntry]] = {}

    def load(self) -> None:
        """Load every constellation the three layers define."""
        documents = load_layered_documents(self._pipelines_dir, self._private_dir,
                                           self._user_overrides_dir, _OVERRIDE_LIST_KEYS,
                                           merge_step=_keep_one_llm_form)
        for document in documents:
            # PipelineConfig(**data) is the Pydantic validation gate — a malformed constellation
            # fails loudly here at load time, not mid-run.
            try:
                declared = PipelineConfig(**document.data)
            except ValidationError as exc:
                # A constellation that fails validation is a configuration refusal, not a crash:
                # surfaced as ConfigurationError so server_cli exits 2 and the service manager
                # does not restart it. As a bare pydantic error it exited 1, and NSSM restarted a
                # mistyped switch-off overlay for seven minutes (the variant switch-off of
                # 2026-09-23). The message names every layer that contributed.
                origin = (' (merged with ' + ', '.join(o.label for o in document.overlays) + ')'
                          if document.overlays else '')
                kind = 'private pipeline config' if document.private else 'pipeline config'
                raise ConfigurationError(
                    f'{kind} {document.name}{origin} is invalid:\n{exc}') from exc
            validated = declared.model_dump()
            for overlay in document.overlays:
                # Collected against the VALIDATED config: a key Pydantic dropped is a typo
                # candidate; a key with a schema default reads as '(added)'.
                self._override_entries[overlay.label] = collect_overrides(
                    overlay.base, overlay.overlay, validated, _OVERRIDE_LIST_KEYS)
            for config in expand_variants(declared):
                # A derived stream id colliding with another pipeline (or another file's
                # expansion, or a private definition reusing a public id) would silently
                # shadow a signal series — refuse.
                if config.pipeline_id in self._pipelines:
                    raise ConfigurationError(
                        f"duplicate pipeline_id '{config.pipeline_id}' "
                        f'(from {document.name}) — stream ids must be unique')
                self._pipelines[config.pipeline_id] = Pipeline(config)
                if document.overlays:
                    self._overridden.add(config.pipeline_id)
                if document.private:
                    self._private.add(config.pipeline_id)

    def is_overridden(self, pipeline_id: str) -> bool:
        """Whether this stream's constellation was patched by the private layer or the machine."""
        return pipeline_id in self._overridden

    def is_private(self, pipeline_id: str) -> bool:
        """Whether this stream is defined by the private layer, not the public reference."""
        return pipeline_id in self._private

    def override_entries(self) -> Dict[str, List[OverrideEntry]]:
        """Touched leaves per applied overlay, keyed by its layer label (empty when none)."""
        return self._override_entries

    def list_pipelines(self) -> List[Pipeline]:
        return list(self._pipelines.values())

    def get(self, pipeline_id: str) -> Pipeline:
        if pipeline_id not in self._pipelines:
            raise PipelineNotFoundError(f"Unknown pipeline_id: '{pipeline_id}'")
        return self._pipelines[pipeline_id]
