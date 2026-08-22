"""Stream-sequence domain types (ISSUE_9) — what the sequencer produces.

Runtime shapes, deliberately `@dataclass` and not Pydantic: unlike `outcome_types`, none of these
is ever serialized. They cross the seam between `StreamSequencer` and its callers (the outcome
store on the write path, the assembler at boot) and stop there — what reaches a consumer are the
envelope fields these values are copied into.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class StreamStamp:
    """One stream position, minted inside the envelope's own insert transaction.

    A result object rather than a tuple because it is a stage boundary: the next field this grows
    (a dispatcher hint, a partition id) must be additive, not a call-site refactor.
    """
    seq: int                                # gapless within the epoch; first envelope of a stream is 1
    epoch: int                              # monotone; a change means the series was rewound
    available_msc: int                      # clamped: max(sampled clock, previous value)
    resyncs: int                            # times the clock stepped back on this stream, cumulative
    max_correction_ms: int                  # largest single correction ever held, cumulative


@dataclass
class EpochBump:
    """One stream whose series was found rewound at boot — always worth a loud log.

    `reason` is the evidence that produced it, not a guess: 'counter_behind_journal' (the counter
    was reset while the journal survived) or 'cluster_changed' (a different
    `<system_identifier>/<timeline_id>` than the one last seen — PITR, promotion, or a restore into
    a fresh cluster). A logical dump/restore in place produces neither and stays a runbook step.
    """
    pipeline_id: str
    previous_epoch: int
    new_epoch: int
    reason: str
    previous_seq: int
    new_seq: int
    cluster_id: Optional[str] = None        # None when pg_control_* was not readable (managed host)
