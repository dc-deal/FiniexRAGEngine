"""Tests for the set section's boot check in the assembler — no DB, no API.

A set section can only fill from a set that is ingested, and ingest workers follow the pipelines'
`source_set`: a section on a set no pipeline is on would stay empty on every pass. The assembler
refuses that at boot and announces every working section in one `[SECTION]` line.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from finiexragengine.configuration.source_set_registry import SourceSetRegistry
from finiexragengine.core.pipeline.pipeline_assembler import PipelineAssembler
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig


class _Pipeline:
    def __init__(self, config: PipelineConfig) -> None:
        self._config = config

    def get_config(self) -> PipelineConfig:
        return self._config


class _Registry:
    """PipelineRegistry stand-in — the check only lists the pipelines."""

    def __init__(self, *configs: PipelineConfig) -> None:
        self._pipelines = [_Pipeline(config) for config in configs]

    def list_pipelines(self) -> List[_Pipeline]:
        return self._pipelines


def _sets(tmp_path: Path) -> SourceSetRegistry:
    for set_id, feeds, scope in (('crypto_news', ['coindesk'], None),
                                 ('general_news', ['feed_a', 'feed_b'], 'declared'),
                                 ('macro_wire', ['wire'], None)):
        data: Dict[str, Any] = {'source_set_id': set_id, 'sources': [
            {'source_id': feed, 'type': 'rss', 'url': f'https://{feed}.test/rss'} for feed in feeds]}
        if scope:
            data['scope'] = scope
        (tmp_path / f'{set_id}.json').write_text(json.dumps(data))
    registry = SourceSetRegistry(tmp_path)
    registry.load()
    return registry


def _pipeline(pipeline_id: str, source_set: str,
              section_set: Optional[str] = None) -> PipelineConfig:
    retrieval = ({'set_section': {'source_set': section_set, 'query': 'world economy', 'top_k': 3,
                                  'floor_distance': 0.7}} if section_set else {})
    return PipelineConfig(pipeline_id=pipeline_id, outcome_type='o', market='crypto',
                          symbols=[{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}],
                          llm={'model': 'gpt-4o-mini'}, source_set=source_set, retrieval=retrieval)


def _assembler(tmp_path: Path) -> PipelineAssembler:
    # __new__ + manual wiring skips every DB connect — the check needs only the source sets.
    assembler = PipelineAssembler.__new__(PipelineAssembler)
    assembler._source_sets = _sets(tmp_path)
    return assembler


def test_a_section_on_the_pipelines_own_set_is_announced(tmp_path, caplog):
    registry = _Registry(_pipeline('macro', 'general_news', section_set='general_news'))
    with caplog.at_level(logging.INFO):
        _assembler(tmp_path)._announce_set_sections(registry)
    assert ('[SECTION] macro · set_section from general_news · 2 active feeds · top_k 3 · floor 0.7'
            in caplog.text)


def test_a_section_on_a_set_no_pipeline_is_on_is_refused(tmp_path):
    # `macro_wire` is shared and valid, but nothing ingests it — the section could only be empty.
    registry = _Registry(_pipeline('macro', 'general_news', section_set='macro_wire'))
    with pytest.raises(ConfigurationError, match='never ingested'):
        _assembler(tmp_path)._announce_set_sections(registry)


def test_a_shared_set_another_pipeline_is_on_is_accepted(tmp_path, caplog):
    registry = _Registry(_pipeline('macro', 'crypto_news', section_set='macro_wire'),
                         _pipeline('wire_reader', 'macro_wire'))
    with caplog.at_level(logging.INFO):
        _assembler(tmp_path)._announce_set_sections(registry)
    assert '[SECTION] macro · set_section from macro_wire · 1 active feeds' in caplog.text


def test_pipelines_without_a_section_say_nothing(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        _assembler(tmp_path)._announce_set_sections(_Registry(_pipeline('plain', 'crypto_news')))
    assert '[SECTION]' not in caplog.text
