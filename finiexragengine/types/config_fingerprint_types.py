"""The identity of one resolved engine configuration (ISSUE_85)."""
from dataclasses import dataclass


@dataclass
class ConfigFingerprint:
    """What a pipeline's inputs and scoring setup were, reduced to one comparable scalar.

    The configuration twin of `PromptMetadata`: `prompt_hash` fingerprints the template,
    this fingerprints everything that feeds it. `value` is stamped on every envelope, so a
    consumer compares two archive days with one equality check instead of shipping the
    configuration itself; `canonical` is the exact serialization the hash was taken over and
    is persisted next to it, so a past fingerprint stays explainable long after the config
    file moved on.

    Resolved once at assembly and carried, never recomputed per pass — it describes the
    configuration the process *loaded*, which is what actually produced the envelopes.
    """
    value: str            # 12-char sha256 prefix — the envelope's `config_fingerprint`
    canonical: str        # the serialization that was hashed (sorted, separator-stable JSON)
    pipeline_id: str      # the stream this was resolved for
    source_set_id: str    # the resolved source-set behind it
