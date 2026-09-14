"""`GET /v1/pipelines` — the listing, and the one number it must not derive itself (ISSUE_9).

The cadence is served on two surfaces: here as `cadence_seconds`, and on `/v1/health` as the eval
worker's `interval_seconds`. The consumer reads it to compute a staleness threshold, so the two
must be one fact. They were two derivations — this file is what keeps them one.
"""
import json
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.testclient import TestClient

from finiexragengine.api.endpoints.pipelines_router import build_pipelines_router
from finiexragengine.core.pipeline.eval_worker import EvalWorker
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.core.triggers.interval_trigger import IntervalTrigger
from finiexragengine.types.config_types.app_config_types import StreamConfig

_BASE: Dict[str, Any] = {
    'pipeline_id': 'bar_close', 'outcome_type': 'sentiment_fear_greed', 'market': 'crypto',
    'symbols': [{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD'}],
    'source_set': 'crypto_news', 'llm': {'model': 'gpt-4o-mini'},
}
# M15, deliberately not the M10 both live pipelines run: a wrong conversion is invisible against a
# value that is also the schema default for `interval_seconds`.
_BAR_CLOSE = {**_BASE, 'trigger': {'type': 'interval', 'timeframe': 'M15'}}
# No timeframe, so the cadence comes from the raw interval. This is the case the router's own
# converter reported as `null` while `/health` reported the number — the divergence this fix removes.
_INTERVAL_ONLY = {**_BASE, 'pipeline_id': 'interval_only',
                  'trigger': {'type': 'interval', 'interval_seconds': 300}}


def _registry(tmp_path: Path, *configs: Dict[str, Any]) -> PipelineRegistry:
    directory = tmp_path / 'pipelines'
    directory.mkdir(parents=True, exist_ok=True)
    for config in configs:
        (directory / f'{config["pipeline_id"]}.json').write_text(json.dumps(config))
    registry = PipelineRegistry(directory)
    registry.load()
    return registry


def _payload(registry: PipelineRegistry,
             stream: StreamConfig = StreamConfig()) -> Dict[str, Any]:
    app = FastAPI()
    app.include_router(build_pipelines_router(registry, stream))
    response = TestClient(app).get('/v1/pipelines')
    assert response.status_code == 200
    return response.json()


def _listing(registry: PipelineRegistry) -> Dict[str, Dict[str, Any]]:
    return {entry['pipeline_id']: entry for entry in _payload(registry)['pipelines']}


def test_the_cadence_comes_from_the_trigger_property_not_from_a_second_conversion(
        tmp_path: Path) -> None:
    """Both shapes `TriggerConfig.cadence_seconds` resolves — a bar-close frame and a raw interval.

    The second one is what discriminates: the router used to convert `trigger.timeframe` itself and
    answered `null` whenever there was none, while the same pipeline's eval worker answered the
    fallback. One fact, two derivations, agreeing only where a timeframe happened to be set.
    """
    listing = _listing(_registry(tmp_path, _BAR_CLOSE, _INTERVAL_ONLY))

    assert listing['bar_close']['cadence_seconds'] == 900          # M15, converted once
    assert listing['interval_only']['cadence_seconds'] == 300      # was `null` before


def test_the_listing_and_the_worker_report_the_same_number(tmp_path: Path) -> None:
    """The cross-surface assertion, on the same config: `/v1/pipelines` against what `/health` shows.

    `/health` renders `WorkerState.interval_seconds` straight through, so asserting against the
    worker's own state is asserting against that surface without booting workers (which would make
    this a paid, DB-bound test for a question about one integer).
    """
    registry = _registry(tmp_path, _BAR_CLOSE)
    pipeline = registry.get('bar_close')
    worker_state = EvalWorker(pipeline, IntervalTrigger(interval_seconds=900)).get_state()

    assert _listing(registry)['bar_close']['cadence_seconds'] == worker_state.interval_seconds


def test_the_cadence_is_always_present(tmp_path: Path) -> None:
    """No configuration yields nothing, so the field is not nullable and a consumer needs no branch.

    Asserted on the serialized payload rather than on the model: the consumer parses JSON, and a
    `None` that Pydantic would happily emit for an Optional field is what they would have to guard.
    """
    for entry in _listing(_registry(tmp_path, _BAR_CLOSE, _INTERVAL_ONLY)).values():
        assert isinstance(entry['cadence_seconds'], int)


def test_the_stream_block_reflects_the_running_config_not_a_default(tmp_path: Path) -> None:
    """The whole reason these two numbers are served: the consumer must not configure a second answer.

    A router free to fall back to a schema default could serve numbers the engine is not using — the
    same "one fact, two copies" defect the cadence field above just had, one field over. So the
    values are asserted against a deliberately NON-default configuration; equal-to-default would
    pass even if the parameter were ignored.
    """
    configured = StreamConfig(heartbeat_seconds=7, replay_window_hours=3)

    stream = _payload(_registry(tmp_path, _BAR_CLOSE), configured)['stream']

    assert stream == {'heartbeat_seconds': 7, 'replay_window_hours': 3}


def test_the_stream_block_sits_at_response_level_and_not_on_the_rows(tmp_path: Path) -> None:
    """Engine-wide facts are served once. A per-row copy would claim to be a per-stream property,
    and someone would eventually set two of them differently — the reason `pass_timeout_seconds`
    stays engine-level on `/health` rather than being repeated per pipeline."""
    payload = _payload(_registry(tmp_path, _BAR_CLOSE, _INTERVAL_ONLY))

    assert set(payload['stream']) == {'heartbeat_seconds', 'replay_window_hours'}
    for row in payload['pipelines']:
        assert 'heartbeat_seconds' not in row and 'replay_window_hours' not in row
