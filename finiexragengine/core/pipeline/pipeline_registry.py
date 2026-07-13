"""Loads constellation JSONs into Pipeline objects and serves them by id."""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from finiexragengine.configuration.config_merge import deep_merge
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
# replacing wholesale — so an override can flip a single variant's `enabled` (or a source's
# weight) without restating the whole array. Plain lists (symbols, keywords) still replace.
_OVERRIDE_LIST_KEYS = {'models': 'sub_pipeline_id', 'sources': 'source_id'}


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
    or N logical pipelines when it declares model variants (ISSUE_42).
    """

    def __init__(self, pipelines_dir: Path,
                 user_overrides_dir: Optional[Path] = None) -> None:
        self._pipelines_dir = pipelines_dir
        # Optional gitignored per-pipeline overrides (deep-merged onto the tracked config, same
        # pattern as app_config) — the place for a dev/live variant (fewer symbols, other models)
        # without touching the committed constellation. None = no overrides.
        self._user_overrides_dir = user_overrides_dir
        self._pipelines: Dict[str, Pipeline] = {}
        # Stream ids whose constellation carried a gitignored user override (all variants of
        # an overridden file) — surfaced e.g. in the cost report so a diverging config is visible.
        self._overridden: set = set()

    def load(self) -> None:
        """Load every constellation JSON in the pipelines directory."""
        # Discovery: one constellation JSON = one Pipeline (N with variants), keyed by
        # pipeline_id. PipelineConfig(**data) is the Pydantic validation gate — a
        # malformed constellation fails loudly here at load time, not mid-run.
        for path in sorted(self._pipelines_dir.glob('*.json')):
            data = self._with_override(path)
            overridden = (self._user_overrides_dir is not None
                          and (self._user_overrides_dir / path.name).exists())
            for config in expand_variants(PipelineConfig(**data)):
                # A derived stream id colliding with another pipeline (or another
                # file's expansion) would silently shadow a signal series — refuse.
                if config.pipeline_id in self._pipelines:
                    raise ConfigurationError(
                        f"duplicate pipeline_id '{config.pipeline_id}' "
                        f'(from {path.name}) — stream ids must be unique')
                self._pipelines[config.pipeline_id] = Pipeline(config)
                if overridden:
                    self._overridden.add(config.pipeline_id)

    def is_overridden(self, pipeline_id: str) -> bool:
        """Whether this stream's constellation was deep-merged with a user override."""
        return pipeline_id in self._overridden

    def _with_override(self, path: Path) -> Dict[str, Any]:
        """Load a constellation, deep-merging a gitignored user override when one exists.

        Override only what differs — everything else is inherited from the tracked base (no
        silent drift, unlike a full copy). Lists (`symbols`, `llm.models`) replace wholesale.
        """
        data = json.loads(path.read_text(encoding='utf-8'))
        if self._user_overrides_dir is None:
            return data
        override_path = self._user_overrides_dir / path.name
        if not override_path.exists():
            return data
        override = json.loads(override_path.read_text(encoding='utf-8'))
        merged = deep_merge(data, override, _OVERRIDE_LIST_KEYS)
        # Guard the llm XOR: if the override picks a form (model vs models), drop the base's
        # *other* form so switching forms via override can't produce an invalid "both present".
        override_llm = override.get('llm', {})
        merged_llm = merged.get('llm')
        if isinstance(merged_llm, dict):
            if 'model' in override_llm and 'models' not in override_llm:
                merged_llm.pop('models', None)
            elif 'models' in override_llm and 'model' not in override_llm:
                merged_llm.pop('model', None)
        logger.info("pipeline '%s' overridden from user_configs/pipelines/%s",
                    data.get('pipeline_id', path.stem), path.name)
        return merged

    def list_pipelines(self) -> List[Pipeline]:
        return list(self._pipelines.values())

    def get(self, pipeline_id: str) -> Pipeline:
        if pipeline_id not in self._pipelines:
            raise PipelineNotFoundError(f"Unknown pipeline_id: '{pipeline_id}'")
        return self._pipelines[pipeline_id]
