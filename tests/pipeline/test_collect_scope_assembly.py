"""Collect-only source sets in the assembler (#137) — no DB, no API.

A collect set is ingested to be kept and read by no pipeline: the assembler refuses a pipeline on one,
builds its ingest pass without breaking detection, and says so in one `[SCOPE]` line at boot.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.pipeline import pipeline_assembler as assembler_module
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.app_config_types import AppConfig
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


class _Pipeline:
    def __init__(self, config: PipelineConfig) -> None:
        self._config = config

    def get_config(self) -> PipelineConfig:
        return self._config


class _Registry:
    def __init__(self, *configs: PipelineConfig) -> None:
        self._pipelines = [_Pipeline(config) for config in configs]

    def list_pipelines(self) -> List[_Pipeline]:
        return self._pipelines


def _sets(tmp_path: Path) -> SourceSetRegistry:
    for set_id, feeds, scope in (('crypto_news', ['coindesk'], None),
                                 ('research_feeds', ['feed_a', 'feed_b', 'feed_c'], 'collect')):
        data: Dict[str, Any] = {'source_set_id': set_id, 'sources': [
            {'source_id': feed, 'type': 'rss', 'url': f'https://{feed}.test/rss'} for feed in feeds]}
        if scope:
            data['scope'] = scope
        (tmp_path / f'{set_id}.json').write_text(json.dumps(data))
    registry = SourceSetRegistry(tmp_path)
    registry.load()
    return registry


def _pipeline(pipeline_id: str, source_set: str) -> PipelineConfig:
    return PipelineConfig(pipeline_id=pipeline_id, outcome_type='o', market='crypto',
                          symbols=[{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}],
                          llm={'model': 'gpt-4o-mini'}, source_set=source_set)


def _assembler(tmp_path: Path) -> PipelineAssembler:
    # __new__ + manual wiring skips every DB connect — these checks need only the source sets.
    assembler = PipelineAssembler.__new__(PipelineAssembler)
    assembler._source_sets = _sets(tmp_path)
    assembler._reconcile_stream = lambda pipeline_id: None
    return assembler


def test_a_pipeline_on_a_collect_set_is_refused_before_anything_is_built(tmp_path):
    with pytest.raises(ConfigurationError, match='collect set'):
        _assembler(tmp_path).build_runner(_pipeline('reader', 'research_feeds'))


def test_a_collect_set_is_announced_as_ingested_and_read_by_none(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _assembler(tmp_path)._announce_scopes(_Registry(_pipeline('crypto', 'crypto_news')))
    assert ('[SCOPE] research_feeds · collect · 3 active feeds · ingested, read by no pipeline '
            '· breaking detection off') in caplog.text


class _Stub:
    """Stands in for every connecting collaborator of `build_ingestor`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


def _ingestor(tmp_path: Path, monkeypatch, source_set_id: str):
    for name in ('OpenAIEmbedder', 'PgVectorStore', 'SourceHealthStore', 'SourcePollLog'):
        monkeypatch.setattr(assembler_module, name, _Stub)
    monkeypatch.setattr(assembler_module, 'BreakingDetector', _Stub)
    assembler = _assembler(tmp_path)
    assembler._cfg = AppConfig()
    assembler._recorder = None
    assembler._budget_guard = None
    assembler._database_url = 'postgresql://unused'
    return assembler.build_ingestor(source_set_id)


def test_a_collect_set_is_ingested_without_breaking_detection(tmp_path, monkeypatch):
    collect = _ingestor(tmp_path, monkeypatch, 'research_feeds')
    shared = _ingestor(tmp_path, monkeypatch, 'crypto_news')
    assert collect._breaking_detector is None
    assert isinstance(shared._breaking_detector, _Stub)
    assert len(collect._sources) == 3
