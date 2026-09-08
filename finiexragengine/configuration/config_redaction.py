"""Which configuration leaves may be published, decided per field rather than guessed (2026-09-08).

`GET /v1/configs/{name}` serves the effective configuration so a remote diagnosis can read what a
machine actually runs. The config objects carry three credentials, and the interesting part is how
few: `DATABASE_URL` and `OPENAI_API_KEY` are environment variables and were never in these models
at all, so the surface is small and enumerable rather than open-ended.

**A string is published because its name is written down here.** That is this codebase's own access
rule (`CLAUDE.md`: granted by name, never by omission) applied to a second surface. The failure mode
of the alternative — a denylist of secret-looking names — is a field nobody re-read: Spring Boot
Actuator's default sanitiser (`password|secret|key|token|…`) would mask `bot_token` and publish
`chat_id`, because it is a heuristic about names rather than a decision about fields.

So an unlisted string is masked *and* reported as `unclassified`, which is different from
`redacted`: one says "this is a secret", the other says "nobody has looked at this yet". A reader
who sees the second knows the policy is stale rather than that the field is dangerous — and
`tests/contracts/test_config_exposure.py` fails the moment a model gains a string this file does not
name, so the state is short-lived by construction.

Non-string leaves (numbers, booleans, dates) pass through unlisted: a threshold is the diagnostic
payload itself, and requiring a signature for every `int` would make the policy noise nobody reads.
"""
from typing import Literal, Tuple

# The classification a single leaf gets. 'unclassified' is a state, not an error — see the module
# docstring: it is masked like a secret and reported unlike one.
LeafClass = Literal['public', 'sensitive', 'unclassified']

# `*` matches exactly one path segment: a dict key (`api.tokens.ide`) or a list index
# (`sources.0`). Concrete paths carry the real key; patterns carry the star.
_WILDCARD = '*'

# --- the three that must never leave the process ------------------------------------------------
SENSITIVE_PATHS: Tuple[str, ...] = (
    'api.tokens.*.token',        # the bearer tokens themselves
    'telegram.bot_token',        # the bot's identity — anyone holding it can post as the engine
    'telegram.chat_id',          # the one chat the bot serves; a target, not a credential, but
                                 # publishing it invites exactly one kind of nuisance
)

# --- everything else, one line per string leaf, classified by hand -------------------------------
# The list is long on purpose. It is the record that each of these was looked at, and it is what the
# contract test compares the models against — a field added later is absent here and therefore both
# masked at runtime and red in the suite.
PUBLIC_PATHS: Tuple[str, ...] = (
    # app: identity and shape
    'version',
    'schema_version',
    'log_level',
    'journal_names.*',                  # the names; the keys are journal ids `/v1/health` serves
    # app: the API surface's own description — the boot log already prints all three
    'api.tokens.*.grants.*',
    'api.tokens.*.note',
    'stream.notify_channel',
    # app: the model stack. Score-defining, and three of these are in the config fingerprint —
    # which is precisely why a remote reader needs them.
    'llm.provider',
    'llm.allowed_models.*',
    'llm.base_url',                     # scrubbed by `utils.redaction` if it ever carries userinfo
    'embedding.provider',
    'embedding.model',
    'embedding.encoding',
    'ingest.text_normalizer',
    'vector_store.backend',
    'pricing.currency',
    # app: operational settings
    'logging.file',
    'logging.rotation',
    'logging.quiet_loggers.*',
    'telegram.report_command',
    'weekly_report.day_of_week',
    'weekly_report.timezone',
    'weekly_report.export_dir',
    # The connectivity probe's targets (2026-09-08) — a public DNS name and a literal address the
    # engine dials during an outage. Publishing them is the point: a reader has to know WHICH
    # destination a probe verdict is about before the verdict means anything.
    'diagnostics.connectivity_probe_dns',
    'diagnostics.connectivity_probe_tcp',
    # app: per-report defaults (ISSUE_104) — window strings, one per report
    'reports.source_latency.window',
    'reports.source_quarantine.window',
    'reports.breaking.window',
    'reports.breaking_timeline.window',
    'reports.prompt_drift.window',
    'reports.corpus_text.window',
    'reports.perf.window',
    'reports.cost.windows.*',
    'reports.detection_sweep.window',
    'reports.retrieval_drift.window',
    'reports.detection_quality.window',
    # pipeline: what it is and what it evaluates
    'pipeline_id',
    'outcome_type',
    'market',
    'source_set',
    'variant_group',
    'variant',
    'symbols.*.key',
    'symbols.*.base',
    'symbols.*.quote',
    'symbols.*.query',
    'prompt.name',
    'prompt.version',
    'llm.model',
    'llm.models.*.name',
    'llm.models.*.sub_pipeline_id',
    'trigger.type',
    'trigger.timeframe',
    # source set: the catalogue itself
    'source_set_id',
    'detection.cluster_unit',
    'detection.keywords.*',
    'sources.*.source_id',
    'sources.*.type',
    'sources.*.url',                    # a feed key riding in the query string is scrubbed by
                                        # `utils.redaction`, which is the second layer's whole job
    'sources.*.comment',
)

_SENSITIVE = frozenset(SENSITIVE_PATHS)
_PUBLIC = frozenset(PUBLIC_PATHS)
CLASSIFIED: frozenset = _SENSITIVE | _PUBLIC


def _matches(pattern: str, path: str) -> bool:
    """Segment-wise match where a `*` in the pattern stands for one concrete segment."""
    pattern_parts = pattern.split('.')
    path_parts = path.split('.')
    if len(pattern_parts) != len(path_parts):
        return False
    return all(expected == _WILDCARD or expected == actual
               for expected, actual in zip(pattern_parts, path_parts))


def classify(path: str) -> LeafClass:
    """What may be done with the value at this concrete path.

    Order matters only in that `sensitive` is checked first: a path listed in both would be a
    contradiction, and the test refuses that case rather than letting the order decide it.
    """
    if any(_matches(pattern, path) for pattern in SENSITIVE_PATHS):
        return 'sensitive'
    if any(_matches(pattern, path) for pattern in PUBLIC_PATHS):
        return 'public'
    return 'unclassified'
