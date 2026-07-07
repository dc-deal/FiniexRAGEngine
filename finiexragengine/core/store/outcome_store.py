"""Persistence for pipeline outcomes — the source of truth for backtest replay."""
from typing import Optional

from finiexragengine.types.outcome_types import AnalysisEnvelope


class OutcomeStore:
    """Stores every produced envelope (with its timestamp) and serves the latest.

    The store — not the live socket — is the source of truth: every outcome
    (breaking or not) is persisted so a backtest can replay it deterministically.
    Backfill / replay (ISSUE_4) re-runs analysis over the retained raw-article
    corpus to regenerate a comparable series after a prompt_version change.

    TODO(impl): append envelopes (JSONL or a table); get_latest(pipeline_id).
    """

    def save(self, envelope: AnalysisEnvelope) -> None:
        raise NotImplementedError('OutcomeStore.save')

    def get_latest(self, pipeline_id: str) -> Optional[AnalysisEnvelope]:
        raise NotImplementedError('OutcomeStore.get_latest')
