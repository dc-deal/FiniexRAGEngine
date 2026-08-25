"""Computes the configuration fingerprint stamped on every envelope (ISSUE_85).

`prompt_hash` fingerprints the prompt template, so a silent prompt edit is visible downstream.
Nothing did the same for the **inputs**: a feed added, disabled or re-weighted shifts the score
distribution — the LLM reads a different corpus — while every provenance field stays
byte-identical. In the real archive the symbol set grew on 2026-07-24 without a single field
moving; the only record of it was this repository's git history, which a downstream tool cannot
read. Two archive days are comparable when `prompt_hash` *and* `config_fingerprint` agree.

Three properties this unit exists to guarantee:

1. **Merged, not tracked.** The inputs come from what
   `AppConfigManager.build_pipeline_registry()` / `build_source_set_registry()` produced. A
   fingerprint over the tracked file would describe a configuration that did not run — two
   observed `user_configs/` overrides disable feeds, exactly the change this catches.
2. **The source set is resolved.** `PipelineConfig.source_set` is only a string reference; the
   feeds, weights and detection thresholds live in the referenced set. Hashing the pipeline
   alone would not move when a feed is added.
3. **Canonical form.** Sorted keys, stable separators, and lists ordered by their own
   serialization — a harmless reordering in a config file must not read as a change.

Not a build identifier: two machines with different `user_configs/` overlays legitimately
produce different fingerprints for the same tracked config, because they *are* different
configurations. See `docs/development/user_configs_overrides.md`.

A deliberate function module (like `provider_factory`, `envelope_contract`) rather than a
`utils/` helper — it needs the config types, and `utils/` is dependency-free.
"""
import hashlib
import json
from typing import Any, Dict

from finiexragengine.types.config_fingerprint_types import ConfigFingerprint
from finiexragengine.types.config_types.app_config_types import AppConfig
from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.config_types.source_set_types import SourceSetConfig

# Same width as the prompt fingerprint (`prompt_builder`) — the two sit next to each other on
# every envelope and in every report line, so they share a shape.
_LENGTH = 12

# --- what stays OUT, and why -------------------------------------------------------------
# The two pipeline-scoped configs use a DENYLIST: everything in them describes what is
# produced, so a field is series-relevant until it is excluded here with a reason. A field
# added by a later issue therefore lands in the hash by default — deliberately, because a
# false negative (a change that fails to move the fingerprint) is silent, and silence is the
# exact bug this unit removes. A false positive is visible and can be corrected here.

_PIPELINE_EXCLUDED: Dict[str, str] = {
    'pipeline_id': 'stream identity, not an input — the envelope carries it, and a rename '
                   'does not change what was read',
    'variant_group': 'fan-out naming (ISSUE_42) — the variant\'s model itself is in `llm`',
    'variant': 'fan-out naming (ISSUE_42)',
    # The two episode knobs (ISSUE_82) are the denylist's narrow case, and the reason is worth
    # stating precisely: they change how passes are GROUPED at read time, never what a pass
    # produces. Two runs either side of a retuned gap emit byte-identical envelopes — same
    # `is_breaking`, same `urgency`, same signal — so hashing them would fork a series that did
    # not fork, and would mark every pipeline `(new)` on the deploy that merely retuned a report.
    # `breaking.urgency_threshold` and `breaking.min_importance` stay IN: they decide what the
    # envelope says and which passes run at all.
    'breaking.urgency_exit_threshold': 'episode grouping only (ISSUE_82) — the envelope is '
                                       'unchanged by it, so retuning it must not fork the series',
    'breaking.episode_gap_minutes': 'episode grouping only (ISSUE_82) — a read-time derivation '
                                    'over persisted passes, applied retroactively to the archive',
    'breaking.episode_seed_hours': 'boot-time replay depth (ISSUE_82) — decides what a restart '
                                   'can still SHOW, never what any pass produced',
    'breaking.story_similarity': 'story grouping only (ISSUE_96) — a read-time clustering of '
                                 'episodes already produced, one level above episode_gap_minutes',
    'breaking.story_window_hours': 'story grouping only (ISSUE_96) — how far apart two episodes '
                                   'may be and still count as one story; reporting, not scoring',
}

_SOURCE_SET_EXCLUDED: Dict[str, str] = {
    'trigger': 'ingest poll cadence — pace, not corpus content. NOTE the asymmetry: the '
               'PIPELINE trigger IS hashed, because eval cadence (the bar-close timeframe) is '
               'series-defining. Same key name, two different clocks',
    'fetch_timeout_seconds': 'operational deadline (ISSUE_73) — retuning it must never fork a '
                             'comparable series',
}

_SOURCE_EXCLUDED: Dict[str, str] = {
    'poll_interval_seconds': 'per-feed pace',
    'timeout_seconds': 'per-feed operational deadline (ISSUE_73)',
    'comment': 'editorial note about the feed; no effect on what is ingested',
}

# --- what comes IN from app_config, and why ----------------------------------------------
# The inverse rule: `app_config.json` is mostly operational config of the *process*, so this
# half is an ALLOWLIST — a new operational knob must never fork a comparable series just
# because someone added it. These five leaves are score-defining but live outside the pipeline
# file, and they sit in the layer most likely to differ silently between dev box and server.
# Deliberately out: `embedding.max_input_tokens`/`encoding` (model-bound — they travel with
# `embedding.model`, which is in), `llm.timeout_seconds` (a timeout produces an error, not a
# different score), `llm.allowed_models` (governance gate; the model actually used is in the
# pipeline half), `vector_store.*` (the live retrieval knobs are `pipeline.retrieval`), and
# everything else — pricing, budgets, telegram, logging, diagnostics. No credentials, ever.

_APP_INCLUDED: Dict[str, str] = {
    'llm.provider': 'a different protocol/backend is a different scorer',
    'llm.temperature': 'directly shapes the score for identical input',
    'llm.base_url': 'a self-hosted endpoint serving the same model id is not the same model',
    'embedding.model': 'changes the vectors, hence what retrieval selects',
    'embedding.dimensions': 'ditto — travels with the model',
}


def compute_config_fingerprint(pipeline: PipelineConfig, source_set: SourceSetConfig,
                               app: AppConfig) -> ConfigFingerprint:
    """Fingerprint one resolved configuration: merged pipeline + resolved source set + app slice.

    Returns the result object rather than a bare string: the canonical payload travels with the
    hash so the registry can persist what a fingerprint stood for (ISSUE_85), and the identity
    columns come along for free.
    """
    payload = {
        'app': _app_slice(app),
        'pipeline': _prune(pipeline.model_dump(mode='json'), _PIPELINE_EXCLUDED),
        'source_set': _source_set_half(source_set),
    }
    canonical = _dumps(_canonical(payload))
    value = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:_LENGTH]
    return ConfigFingerprint(value=value, canonical=canonical,
                             pipeline_id=pipeline.pipeline_id,
                             source_set_id=source_set.source_set_id)


def _app_slice(app: AppConfig) -> Dict[str, Any]:
    """The score-defining leaves of the app config, flattened to `block.leaf` keys."""
    data = app.model_dump(mode='json')
    return {path: _leaf(data, path) for path in _APP_INCLUDED}


def _source_set_half(source_set: SourceSetConfig) -> Dict[str, Any]:
    """The resolved set minus its acquisition knobs, and each feed minus its own pace knobs.

    Hashes what *runs*, not what is declared (ISSUE_107): the feed list comes from
    `active_sources()`, the same one definition the ingestor and `SourceReach` read. Both
    directions matter and they are not symmetric — disabling a *running* feed still moves the
    fingerprint (it leaves the active set, which is the documented case this field exists for),
    while *declaring* a candidate that is already switched off does not, because a feed the
    engine never builds cannot change what was ingested. Without that, adding a disabled
    candidate to the catalogue forked the signal series for a provable no-op.
    """
    data = _prune(source_set.model_dump(mode='json'), _SOURCE_SET_EXCLUDED)
    data['sources'] = [_prune(source.model_dump(mode='json'), _SOURCE_EXCLUDED)
                       for source in source_set.active_sources()]
    return data


def _leaf(data: Dict[str, Any], path: str) -> Any:
    """Resolve a dotted `block.leaf` path — the allowlist keeps its reasons next to its keys."""
    value: Any = data
    for key in path.split('.'):
        value = value[key]
    return value


def _prune(data: Dict[str, Any], excluded: Dict[str, str]) -> Dict[str, Any]:
    """Drop the excluded keys; the reasons live in the constants above.

    A plain key drops a whole top-level block; a **dotted** key (`breaking.episode_gap_minutes`)
    drops one leaf out of one, so an exclusion can be as narrow as its reason. Without that, a
    block holding both series-defining and report-only knobs would have to be excluded whole or
    not at all — and `breaking` is exactly such a block (ISSUE_82). One level of nesting is
    deliberate: the configs are two levels deep, and a general path walker would invite exclusions
    too fine-grained to reason about.
    """
    pruned = {key: value for key, value in data.items() if key not in excluded}
    for path in excluded:
        head, dot, leaf = path.partition('.')
        if not dot:
            continue
        block = pruned.get(head)
        if isinstance(block, dict) and leaf in block:
            block = dict(block)                 # copy — never mutate the caller's dump
            del block[leaf]
            pruned[head] = block
    return pruned


def _canonical(value: Any) -> Any:
    """Order-independent form. Dict key order is handled by `sort_keys` at dump time; list
    order is handled here, by sorting every list by the serialization of its own elements.

    One rule for all four lists that occur (`symbols`, `llm.models`, `sources`,
    `detection.keywords`) instead of a per-field table — and it matches how the override layer
    already reads them: `_OVERRIDE_LIST_KEYS` patches them by id, i.e. they are id-keyed maps,
    not ordered sequences. Reordering one in a file is not a configuration change.
    """
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return sorted((_canonical(item) for item in value), key=_dumps)
    return value


def _dumps(value: Any) -> str:
    """The one serialization used for both the hash input and the list sort key."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
