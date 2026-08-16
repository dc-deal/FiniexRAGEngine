# Config Overrides (`user_configs/`)

Every tracked config can be overlaid by a gitignored local file under `user_configs/` —
the place for secrets, machine-specific switches, and local experiments. The tracked
files in `configs/` stay the shared defaults; the override layer is deep-merged on top
at load, per machine, and is never committed.

## What can be overridden

| Override file | Base file | Typical use |
|---|---|---|
| `user_configs/app_config.json` | `configs/app_config.json` | secrets (telegram token), budgets, `llm.allowed_models` (admit a `ft:...` model), `llm.base_url` (self-hosted endpoint) |
| `user_configs/pipelines/<id>.json` | `configs/pipelines/<id>.json` | thin a symbol list, flip a model variant off, try a retrieval floor |
| `user_configs/source_sets/<id>.json` | `configs/source_sets/<id>.json` | disable a feed this machine's egress IP cannot reach |

**Prompts are deliberately not overridable** — a prompt is series-defining
(`prompt_version`); a silent per-machine prompt would fork the score series invisibly.

## Merge semantics

One merge, one place: `configuration/config_merge.py`. An override file states **only
what it changes** — everything else is inherited from the base.

- **Nested dicts merge recursively** — `{"llm": {"budget_usd": 5}}` touches one leaf.
- **Plain lists and scalars replace wholesale** — `symbols`, `keywords`,
  `allowed_models`: stating the list means stating *all* of it.
- **Id-keyed lists patch by id** — `models` (by `sub_pipeline_id`) and `sources` (by
  `source_id`): a matching item is patched in place, a new id is appended, untouched
  base items are kept. So one variant flips off without restating the whole array:

```json
// user_configs/pipelines/crypto_sentiment.json
{
    "llm": {
        "models": [
            { "sub_pipeline_id": "4o_enhanced", "enabled": false }
        ]
    }
}
```

## Load paths — there is exactly one per surface

- `AppConfigManager()` merges `user_configs/app_config.json` automatically on
  construction (Pydantic validates the *merged* result — a malformed override fails loudly).
- `AppConfigManager.build_pipeline_registry()` / `build_source_set_registry()` are the
  **only** ways to load constellations and source sets. Call sites must never assemble a
  registry themselves — four CLIs once did and silently dropped the override merge. The
  raw registry constructors remain for tests only.

## The override layer is part of the signal's provenance (ISSUE_85)

Every envelope carries a `config_fingerprint` computed from the **merged** configuration with
the source set **resolved** — so what this layer changes travels with the data instead of
living only in the operator's head. Two of the overrides this project actually runs disable
feeds, which is precisely the kind of change that shifts scores while every other provenance
field stays byte-identical.

Three consequences worth knowing before someone "fixes" one of them:

- **A fingerprint is machine-specific, and that is correct.** The server and a dev container
  carry different overlays, so they produce different fingerprints for the same tracked config.
  It is not a build identifier and must never be turned into one.
- **Operational overrides do not move it.** Timeouts, poll intervals, budgets, logging and
  diagnostics are excluded on purpose — otherwise tuning a timeout would fork a comparable
  series and the marker would be ignored within a week. What is in and what is out (with the
  reason for each) lives in `configuration/config_fingerprint.py`.
- **A config edit only takes effect at the next boot** — for the engine and for the
  fingerprint alike. The fingerprint describes the configuration the process *loaded*, so a
  running engine keeps stamping the truth until it is restarted.

## The startup override report

Every applied override is logged once per process, one line per override file, leaf by leaf:

```
[OVERRIDE] pipelines/crypto_sentiment.json · symbols 8→2 · models[4o_enhanced].enabled true→false
```

- **Old→new per leaf**; string values render as `~changed`/`~added` (prose carries no
  diff signal); more than six leaves collapse to `+N more`.
- **Typo detection:** the Pydantic configs drop unknown keys silently, so a typo'd
  override key would otherwise do nothing without a trace. The report checks each leaf
  against the *validated* merged config and flags misses as `⚠ floor_distanze?`.
- **Gate:** `logging.warn_on_override` in `app_config.json` (default `true`).
- **Boot order:** the app-config report happens before `configure_logging` (the manager is
  constructed first), so it is buffered and replayed into the log once handlers exist. Without
  that it reached only Python's `lastResort` handler — bare on stderr, absent from the rotating
  file, invisible in live mode. Found in a live server log on 2026-08-16.
- `coverage_cli` additionally marks its header with `(+ user override)` when the
  effective pipeline config diverges from the tracked one.

## Conventions

- **Secrets live only here or in `.env`** — never in a tracked file, never in an issue.
- **A leftover override steers every surface** (API, workers, all CLIs) — delete
  experiments once done; the startup report is the safety net, not the cleanup.
- Overrides are **per-machine state**: the server and a dev container each carry their
  own `user_configs/`, and they legitimately differ.
