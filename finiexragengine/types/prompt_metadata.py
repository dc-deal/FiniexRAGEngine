"""Prompt metadata parsed from a template's front-matter (ISSUE_33)."""
from dataclasses import dataclass


@dataclass
class PromptMetadata:
    """The immutable identity of one versioned prompt template.

    Parsed from the `---` YAML front-matter of `prompts/<name>_v<version>.md`. A prompt
    is set in stone per version: its `id` + `version` name the series, and `content_hash`
    fingerprints the *body* (front-matter excluded) so a behaviour-changing edit is visible
    in the output even when the version was not bumped — while a cosmetic metadata fix
    (author typo, description) does not move the hash. Recorded alongside every outcome
    so a consumer can tell exactly which prompt produced a score (replay/backfill).
    """
    id: str
    version: str
    author: str
    created: str
    description: str
    content_hash: str
