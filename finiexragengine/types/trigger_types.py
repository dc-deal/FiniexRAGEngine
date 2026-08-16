"""Why a pass ran — the trigger vocabulary shared by triggers, workers, CLIs and the API (ISSUE_87).

A closed vocabulary rather than free text: the value is written into every envelope and every
cost row, so it is effectively public in the archive and a consumer filters on it. Renaming a key
later breaks them.

Note the deliberate asymmetry with `RunMetadata.trigger_reason`, which is a plain `str`: strict at
the producing seam (a typo fails here), permissive at the parsing boundary (an archived envelope
carrying a value a later version introduced must still load — the envelope contract's "always
parseable" rule outranks type strictness). Same split as `RunError.type`: fixed taxonomy, plain
column.
"""
from typing import Literal, Tuple, get_args

TriggerReason = Literal[
    'scheduled',    # the planned tick — bar close (eval) or interval (ingest)
    'boot',         # the first pass after process start, before the first wait
    'breaking',     # an out-of-band wake over the breaking bus (ISSUE_11)
    'manual',       # run_cli / ingest_cli — the operator at the console
    'external',     # POST /v1/pipelines/{id}/run — a foreign caller
]

# The same vocabulary as data, for validation and for surfaces that enumerate it. '' is not part
# of it: an absent reason means "unknown, produced before this field existed", never a category.
TRIGGER_REASONS: Tuple[str, ...] = get_args(TriggerReason)
