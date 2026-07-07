"""Loads constellation JSONs into Pipeline objects and serves them by id."""
import json
from pathlib import Path
from typing import Dict, List

from finiexragengine.core.pipeline.pipeline import Pipeline
from finiexragengine.exceptions.ragengine_errors import PipelineNotFoundError
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


class PipelineRegistry:
    """Discovers and holds the configured pipelines.

    Each *.json in the pipelines directory is one constellation -> one Pipeline.
    """

    def __init__(self, pipelines_dir: Path) -> None:
        self._pipelines_dir = pipelines_dir
        self._pipelines: Dict[str, Pipeline] = {}

    def load(self) -> None:
        """Load every constellation JSON in the pipelines directory."""
        for path in sorted(self._pipelines_dir.glob('*.json')):
            data = json.loads(path.read_text(encoding='utf-8'))
            config = PipelineConfig(**data)
            self._pipelines[config.pipeline_id] = Pipeline(config)

    def list_pipelines(self) -> List[Pipeline]:
        return list(self._pipelines.values())

    def get(self, pipeline_id: str) -> Pipeline:
        if pipeline_id not in self._pipelines:
            raise PipelineNotFoundError(f"Unknown pipeline_id: '{pipeline_id}'")
        return self._pipelines[pipeline_id]
