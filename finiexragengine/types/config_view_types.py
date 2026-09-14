"""What a config view hands to the API (2026-09-08) — the shape, not the projection.

`GET /v1/configs/{name}` exists because the one layer that differs between the dev container and the
live server is the one nothing exposed: `user_configs/` is gitignored, so which feeds a machine has
switched off, which model variant is disabled and which thresholds it actually runs were invisible
from anywhere but an RDP session.

These shapes cross the `configuration/` → `api/` seam, so they live here rather than with the views
that build them. They deliberately carry the *census* alongside the payload: a reader trusts a
document, so one that was altered has to say where.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class OverrideLeaf:
    """One leaf the gitignored overlay moved, as the boot report already collected it.

    `was` is absent (None) when the tracked file has no such key — the "added" case. `unknown` marks
    a key the validated config does not have: Pydantic ignores unknown keys, so `floor_distanze`
    silently does nothing, and saying so here is the same service the `[OVERRIDE]` boot line does.
    """
    path: str
    value: Any
    was: Any = None
    added: bool = False          # the tracked file had no such key at all
    unknown: bool = False        # not in the validated config — a typo, or a key since removed


@dataclass
class ConfigDocument:
    """One config domain as it is published: effective values, provenance, and what was masked."""
    name: str                                        # 'app' | 'pipelines' | 'source_sets'
    summary: str
    # The files this document was merged from, tracked layer first — so a reader can see that an
    # overlay exists even when it changed nothing.
    layers: List[str] = field(default_factory=list)
    # The effective documents, keyed by id ('app' for the single app config).
    documents: Dict[str, Any] = field(default_factory=dict)
    overrides: Dict[str, List[OverrideLeaf]] = field(default_factory=dict)
    # Paths whose value was replaced because the policy calls them secret.
    redacted: List[str] = field(default_factory=list)
    # Paths masked because nobody has classified them yet — a different statement, and a much
    # shorter-lived one: the contract test fails as soon as a model grows a string the policy does
    # not name. Reported separately so a reader can tell a secret from a gap in the policy.
    unclassified: List[str] = field(default_factory=list)
    # Strings a *pattern* changed rather than the path policy — a credential that reached a field
    # nobody expected to hold one (a feed URL carrying its own key).
    scrubbed: List[str] = field(default_factory=list)
