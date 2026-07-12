"""Loads constellation JSONs into Pipeline objects and serves them by id."""
import json
from pathlib import Path
from typing import Dict, List

from finiexragengine.core.pipeline.pipeline import Pipeline
from finiexragengine.exceptions.ragengine_errors import (
    ConfigurationError,
    PipelineNotFoundError,
)
from finiexragengine.types.config_types.pipeline_config_types import (
    PipelineConfig,
    PipelineLlmConfig,
)


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

    def __init__(self, pipelines_dir: Path) -> None:
        self._pipelines_dir = pipelines_dir
        self._pipelines: Dict[str, Pipeline] = {}

    def load(self) -> None:
        """Load every constellation JSON in the pipelines directory."""
        # Discovery: one constellation JSON = one Pipeline (N with variants), keyed by
        # pipeline_id. PipelineConfig(**data) is the Pydantic validation gate — a
        # malformed constellation fails loudly here at load time, not mid-run.
        for path in sorted(self._pipelines_dir.glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            for config in expand_variants(PipelineConfig(**data)):
                # A derived stream id colliding with another pipeline (or another
                # file's expansion) would silently shadow a signal series — refuse.
                if config.pipeline_id in self._pipelines:
                    raise ConfigurationError(
                        f"duplicate pipeline_id '{config.pipeline_id}' "
                        f'(from {path.name}) — stream ids must be unique')
                self._pipelines[config.pipeline_id] = Pipeline(config)

    def list_pipelines(self) -> List[Pipeline]:
        return list(self._pipelines.values())

    def get(self, pipeline_id: str) -> Pipeline:
        if pipeline_id not in self._pipelines:
            raise PipelineNotFoundError(f"Unknown pipeline_id: '{pipeline_id}'")
        return self._pipelines[pipeline_id]
