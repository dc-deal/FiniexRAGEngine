"""Configuration fingerprint (ISSUE_85) — what moves it, and what must never move it.

The unit is trivial to write and easy to get subtly wrong, so the tests are written as the
*contract* rather than as coverage: each one names an edit an operator really makes and pins
whether two archive days stay comparable across it.

Loaded through the real registry factories wherever the `user_configs/` overlay is in play —
a fingerprint taken from the tracked file alone would describe a configuration that did not run,
which is the trap the issue exists for.
"""
import copy
import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.configuration.config_fingerprint import compute_config_fingerprint
from finiexragengine.types.config_types.app_config_types import AppConfig
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.outcome_types import SentimentEnvelope

# --- fixtures: one small, complete pair, edited per test -----------------------------------

_PIPELINE: Dict[str, Any] = {
    'pipeline_id': 'crypto_sentiment', 'outcome_type': 'sentiment_fear_greed',
    'market': 'crypto',
    'symbols': [{'key': 'BTCUSD', 'base': 'BTC', 'quote': 'USD', 'query': 'Bitcoin BTC'},
                {'key': 'ETHUSD', 'base': 'ETH', 'quote': 'USD', 'query': 'Ethereum ETH'}],
    'prompt': {'name': 'crypto_sentiment', 'version': '2'},
    'llm': {'model': 'gpt-4o-mini'},
    'trigger': {'type': 'interval', 'timeframe': 'M10'},
    'source_set': 'crypto_news',
    'retrieval': {'top_k': 12, 'recency_window_minutes': 1440,
                  'dedup_similarity': 0.85, 'floor_distance': 0.70},
    'breaking': {'urgency_threshold': 0.8, 'min_importance': 2},
    'output_guard': {'score_signal_tolerance': 0.1, 'hold_confidence_max': 0.9},
}

_SOURCE_SET: Dict[str, Any] = {
    'source_set_id': 'crypto_news',
    'trigger': {'type': 'interval', 'interval_seconds': 15},
    'detection': {'cluster_similarity': 0.85, 'keywords': ['hack', 'exploit', 'depeg']},
    'sources': [
        {'source_id': 'coindesk', 'type': 'rss', 'url': 'https://a.test/rss', 'weight': 1.0},
        {'source_id': 'decrypt', 'type': 'rss', 'url': 'https://b.test/feed', 'weight': 0.8},
    ],
}


def _fingerprint(pipeline: Dict[str, Any] = None, source_set: Dict[str, Any] = None,
                 app: Dict[str, Any] = None) -> str:
    """Fingerprint the fixture pair with the given edits already applied."""
    return compute_config_fingerprint(
        PipelineConfig(**(pipeline if pipeline is not None else _PIPELINE)),
        SourceSetConfig(**(source_set if source_set is not None else _SOURCE_SET)),
        AppConfig(**(app or {}))).value


def _edited(base: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(base)


@pytest.fixture
def baseline() -> str:
    return _fingerprint()


# --- canonical form: a reordering is not a change ------------------------------------------

def test_reordering_keys_and_lists_leaves_the_fingerprint_untouched(baseline):
    # JSON has no meaningful order: keys reshuffled by an editor, feeds moved around in the
    # catalogue, symbols listed the other way. None of it changes what the engine reads — and a
    # marker that cried wolf over formatting would be ignored within a week.
    pipeline = {key: _PIPELINE[key] for key in sorted(_PIPELINE, reverse=True)}
    pipeline['symbols'] = list(reversed(pipeline['symbols']))
    source_set = _edited(_SOURCE_SET)
    source_set['sources'] = list(reversed(source_set['sources']))
    source_set['detection']['keywords'] = list(reversed(source_set['detection']['keywords']))
    assert _fingerprint(pipeline=pipeline, source_set=source_set) == baseline


# --- the pipeline half ---------------------------------------------------------------------

def test_adding_a_symbol_changes_it(baseline):
    # The 2026-07-24 archive event: crypto_sentiment gained DOTUSD, forex six pairs, and every
    # provenance field stayed byte-identical. This is the case the issue was filed for.
    pipeline = _edited(_PIPELINE)
    pipeline['symbols'].append({'key': 'DOTUSD', 'base': 'DOT', 'quote': 'USD',
                                'query': 'Polkadot DOT'})
    assert _fingerprint(pipeline=pipeline) != baseline


def test_disabling_a_symbol_changes_it(baseline):
    # A disabled symbol is coverage lost, exactly like an added one is coverage gained.
    pipeline = _edited(_PIPELINE)
    pipeline['symbols'][1]['enabled'] = False
    assert _fingerprint(pipeline=pipeline) != baseline


def test_retrieval_prompt_model_and_eval_cadence_all_change_it(baseline):
    # The four series-defining knobs of the pipeline half. `floor_distance` is the one ISSUE_55
    # will start writing automatically — a retuned floor really does shift the distribution.
    for path, value in ((('retrieval', 'floor_distance'), 0.68),
                        (('retrieval', 'top_k'), 20),
                        (('prompt', 'version'), '3'),
                        (('llm', 'model'), 'gpt-4o'),
                        (('trigger', 'timeframe'), 'H1'),
                        (('breaking', 'urgency_threshold'), 0.6),
                        (('output_guard', 'hold_confidence_max'), 0.75)):
        pipeline = _edited(_PIPELINE)
        pipeline[path[0]][path[1]] = value
        assert _fingerprint(pipeline=pipeline) != baseline, f'{path} must move the fingerprint'


def test_stream_naming_does_not_change_it(baseline):
    # Identity is not an input: a renamed stream reads the same feeds. The envelope carries
    # `pipeline_id` and the ISSUE_42 variant hints separately, so the hash need not repeat them.
    pipeline = _edited(_PIPELINE)
    pipeline['pipeline_id'] = 'crypto_sentiment_renamed'
    pipeline['variant_group'] = 'crypto_sentiment'
    pipeline['variant'] = 'mini'
    assert _fingerprint(pipeline=pipeline) == baseline


# --- the resolved source-set half ----------------------------------------------------------

def test_feed_added_disabled_or_reweighted_changes_it(baseline):
    # Trap 2 of the issue: the pipeline only *references* its set by id, so a fingerprint over
    # the pipeline config alone would sleep through exactly the change most likely to happen.
    added = _edited(_SOURCE_SET)
    added['sources'].append({'source_id': 'theblock', 'type': 'rss',
                             'url': 'https://c.test/rss.xml', 'weight': 1.0})
    disabled = _edited(_SOURCE_SET)
    disabled['sources'][0]['enabled'] = False
    reweighted = _edited(_SOURCE_SET)
    reweighted['sources'][1]['weight'] = 0.2
    repointed = _edited(_SOURCE_SET)
    repointed['sources'][0]['url'] = 'https://a.test/rss2'
    for label, edit in (('added', added), ('disabled', disabled),
                        ('reweighted', reweighted), ('repointed', repointed)):
        assert _fingerprint(source_set=edit) != baseline, f'a {label} feed must be visible'


def test_detection_thresholds_and_vocabulary_change_it(baseline):
    # Detection decides which stories wake an out-of-band eval, so it shapes the series.
    # ISSUE_46 will refresh `keywords` by LLM — that write is meant to move the fingerprint.
    thresholds = _edited(_SOURCE_SET)
    thresholds['detection']['cluster_similarity'] = 0.9
    vocabulary = _edited(_SOURCE_SET)
    vocabulary['detection']['keywords'].append('rugpull')
    assert _fingerprint(source_set=thresholds) != baseline
    assert _fingerprint(source_set=vocabulary) != baseline


def test_the_confirm_gate_changes_it_but_the_episode_knobs_do_not(baseline):
    """ISSUE_82 split one config block across the denylist boundary — this pins the split.

    `urgency_threshold` decides what the envelope *says* (`is_breaking`), so retuning it forks a
    comparable series. `urgency_exit_threshold` and `episode_gap_minutes` only decide how passes
    are grouped into episodes when a report reads them back: two runs either side of a retune emit
    byte-identical envelopes, so hashing them would mark every pipeline `(new)` for a change that
    produced nothing new. That is also why they are the first *dotted* exclusions — the block they
    sit in stays hashed.
    """
    confirm_gate = _edited(_PIPELINE)
    confirm_gate['breaking']['urgency_threshold'] = 0.75
    assert _fingerprint(pipeline=confirm_gate) != baseline

    for knob, value in (('urgency_exit_threshold', 0.6), ('episode_gap_minutes', 50)):
        regrouped = _edited(_PIPELINE)
        regrouped['breaking'][knob] = value
        assert _fingerprint(pipeline=regrouped) == baseline, \
            f'retuning {knob} regroups a report, it does not fork the series'


def test_acquisition_pace_and_timeouts_do_not_change_it(baseline):
    # The whole operational surface at once: per-feed pace, per-feed and set-wide deadlines, the
    # ingest cadence, and the editorial comment. Retuning any of these is a maintenance action —
    # if it forked the series, nobody would dare tune a timeout again.
    source_set = _edited(_SOURCE_SET)
    source_set['trigger']['interval_seconds'] = 60
    source_set['fetch_timeout_seconds'] = 25
    source_set['sources'][0]['poll_interval_seconds'] = 120
    source_set['sources'][0]['timeout_seconds'] = 30
    source_set['sources'][0]['comment'] = 'rewritten editorial note about this feed'
    assert _fingerprint(source_set=source_set) == baseline


# --- the app-config slice ------------------------------------------------------------------

def test_score_defining_app_leaves_change_it(baseline):
    # These live outside the pipeline file but shape the score all the same — and they sit in
    # `user_configs/app_config.json`, the layer most likely to differ silently between machines.
    for block, leaf, value in (('llm', 'temperature', 0.7),
                               ('llm', 'provider', 'vllm'),
                               ('llm', 'base_url', 'http://localhost:8000/v1'),
                               ('embedding', 'model', 'text-embedding-3-large'),
                               ('embedding', 'dimensions', 3072)):
        assert _fingerprint(app={block: {leaf: value}}) != baseline, f'{block}.{leaf}'


def test_operational_app_config_does_not_change_it(baseline):
    # The inverse half of the allowlist: budgets, timeouts, diagnostics, alerting and secrets
    # are how the process is operated, not what it produces.
    assert _fingerprint(app={
        'llm': {'timeout_seconds': 90, 'allowed_models': ['gpt-4o-mini', 'gpt-4o', 'gpt-5']},
        'embedding': {'timeout_seconds': 120, 'max_input_tokens': 4096},
        'cost': {'budget_usd': 25.0, 'account_credit_usd': 100.0},
        'diagnostics': {'poll_log_retention_days': 7},
        'telegram': {'enabled': True, 'bot_token': 'secret', 'chat_id': '4711'},
        'vector_store': {'retrieval_top_k': 99},
        'logging': {'backup_count': 30},
        'log_level': 'DEBUG',
    }) == baseline


# --- the merged-registry trap (the most important one) --------------------------------------

def _write(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding='utf-8')


def _registries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracked: Path,
                user: Path) -> Tuple[PipelineConfig, SourceSetConfig, AppConfig]:
    """Load the fixture pair the way the engine does — through the factories, overlay included."""
    monkeypatch.setattr(AppConfigManager, 'get_pipelines_dir', lambda self: tracked / 'pipelines')
    monkeypatch.setattr(AppConfigManager, 'get_user_pipelines_dir', lambda self: user / 'pipelines')
    monkeypatch.setattr(AppConfigManager, 'get_source_sets_dir', lambda self: tracked / 'sets')
    monkeypatch.setattr(AppConfigManager, 'get_user_source_sets_dir', lambda self: user / 'sets')
    base = tmp_path / 'app_config.json'
    _write(base, {})
    manager = AppConfigManager(config_path=base, user_config_path=tmp_path / 'absent.json')
    pipeline = manager.build_pipeline_registry().get('crypto_sentiment').get_config()
    source_set = manager.build_source_set_registry().get('crypto_news')
    return pipeline, source_set, manager.get_config()


def test_a_user_override_produces_a_different_fingerprint_than_the_tracked_file(
        tmp_path, monkeypatch):
    # Trap 1, and the reason this unit takes resolved config objects rather than file paths:
    # `user_configs/` deep-merges at load, and two overrides observed in production disable
    # feeds. A fingerprint over the tracked file would name a configuration that did not run.
    tracked, user = tmp_path / 'tracked', tmp_path / 'user'
    for root in (tracked, user):
        (root / 'pipelines').mkdir(parents=True)
        (root / 'sets').mkdir(parents=True)
    _write(tracked / 'pipelines' / 'crypto_sentiment.json', _PIPELINE)
    _write(tracked / 'sets' / 'crypto_news.json', _SOURCE_SET)

    pipeline, source_set, app = _registries(tmp_path, monkeypatch, tracked, user)
    without_overlay = compute_config_fingerprint(pipeline, source_set, app).value

    # The exact override the production box carries: one feed switched off per machine.
    _write(user / 'sets' / 'crypto_news.json',
           {'sources': [{'source_id': 'decrypt', 'enabled': False}]})
    pipeline, source_set, app = _registries(tmp_path, monkeypatch, tracked, user)
    with_overlay = compute_config_fingerprint(pipeline, source_set, app).value

    assert source_set.sources[1].enabled is False        # the overlay really did apply
    assert with_overlay != without_overlay


# --- the algorithm itself, and the envelope ------------------------------------------------

def test_golden_value_pins_the_canonicalization():
    # A change to the serialization (separators, sorting, an added/removed field) would fork
    # every series at once and silently: every fingerprint moves although no configuration did.
    # This literal is that alarm. It may only be updated together with a deliberate decision to
    # re-baseline the archive's comparability — never to make a red test green.
    result = compute_config_fingerprint(PipelineConfig(**_PIPELINE),
                                        SourceSetConfig(**_SOURCE_SET), AppConfig())
    assert result.value == '56b4585dbbd9'
    assert result.pipeline_id == 'crypto_sentiment'
    assert result.source_set_id == 'crypto_news'
    # The canonical payload travels with the hash so the registry can persist what it stood for.
    assert result.canonical.startswith('{"app":{"embedding.dimensions":1536,')
    assert '"pipeline_id"' not in result.canonical      # identity is not an input
    assert '"poll_interval_seconds"' not in result.canonical
    assert '"fetch_timeout_seconds"' not in result.canonical


def test_an_envelope_without_the_field_still_parses():
    # Pre-ISSUE_85 archive lines carry no `config_fingerprint`. The exporter re-emits raw stored
    # JSON, so those days will never gain it — absence must stay a valid, readable state.
    archived = {
        'schema_version': '1.0', 'pipeline_id': 'crypto_sentiment',
        'outcome_type': 'sentiment_fear_greed', 'prompt_version': '2',
        'timestamp': '2026-07-22T10:00:00Z', 'status': 'success', 'result': [],
        'metadata': {'model': 'gpt-4o-mini'},
    }
    envelope = SentimentEnvelope.model_validate(archived)
    assert envelope.config_fingerprint == ''            # "unknown", never "same as yesterday"
