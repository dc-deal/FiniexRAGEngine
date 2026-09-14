"""Stream-sequence domain types (ISSUE_9) — what the sequencer produces.

Runtime shapes, deliberately `@dataclass` and not Pydantic: unlike `outcome_types`, none of these
is ever serialized. They cross the seam between `StreamSequencer` and its callers (the outcome
store on the write path, the assembler at boot) and stop there — what reaches a consumer are the
envelope fields these values are copied into.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple, get_args


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


# --- the wire's control vocabulary (ISSUE_9 §3.5) -------------------------------------------
# `control` carries an out-of-band condition as a `code` rather than one event name per case, so a
# new condition is additive and the consumer keeps one handler.
#
# Strict, with no permissive counterpart, and that is not an oversight. The closed-vocabulary rule
# is "strict at the producing seam, permissive at the parsing boundary" — but this vocabulary has no
# parsing boundary on our side: we only ever WRITE these codes. An archived envelope can carry a tag
# a later version introduced; a control frame cannot, because it does not outlive its connection.
#
# `epoch_changed` and `cursor_ahead` stay distinct on purpose: same remedy (resync), different
# diagnosis and different operator alert — the first means WE rewound, the second means the consumer
# did.
ControlCode = Literal['live', 'replay_truncated', 'cursor_ahead', 'epoch_changed', 'auth_revoked']
CONTROL_CODES: Tuple[str, ...] = get_args(ControlCode)


@dataclass
class StreamHead:
    """Where a stream currently stands, as the sequencer's counter knows it (ISSUE_9).

    A result object rather than a tuple because it is a stage boundary the dispatcher, the replay
    policy and the heartbeat all read — and the next field it grows (a dispatcher lag, a last-commit
    timestamp) has to be additive.

    `seq: 0` with `epoch: 0` and no `available_msc` is the **cold stream**: it exists and has never
    produced an envelope. That is a different state from "does not exist", which is a 404 on connect.
    """
    seq: int                                # last committed position; 0 = nothing produced yet
    epoch: int                              # 0 only on a stream the sequencer has never seen
    available_msc: Optional[int]            # None on a cold stream — nothing became fetchable yet


@dataclass
class ControlFrame:
    """One `control` condition: its code plus whatever that code carries (ISSUE_9 §3.5).

    A shape rather than a rendered string, because the policy that *decides* a condition and the
    renderer that *writes* it are different layers — and the API's range endpoint answers the same
    three conditions as JSON rather than as frames.
    """
    code: str                               # from CONTROL_CODES, checked at the renderer
    fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayPlan:
    """What a connect resolves to, before a single byte is written (ISSUE_9 §3.3).

    One object for all three entry points — `?history=N`, `?since=&epoch=`, and neither — so the
    router does no policy and the range endpoint can answer the identical conditions over plain
    HTTP. The order the caller must respect is the field order below: control first (it may be the
    whole answer), then the envelopes, then `live`.
    """
    head: StreamHead
    # A condition that either replaces the replay or precedes it. `replay_truncated` precedes;
    # `epoch_changed` and `cursor_ahead` replace it and are terminal.
    control: Optional[ControlFrame] = None
    # The frames to send before going live, ascending by `seq`. Raw stored JSON, never re-validated.
    envelopes: List[Dict[str, Any]] = field(default_factory=list)
    # Whether `control`/`live` closes the replay. False exactly when `terminal` is True.
    emit_live: bool = True
    # Emit the control frame, then close. The consumer's cursor is unusable, and a connection that
    # neither replays nor goes live would be a third state with no handler on either side.
    terminal: bool = False


@dataclass
class StreamSubscription:
    """One live connection's slot in the dispatcher's fan-out (ISSUE_9 §3.4).

    The queue is **bounded**, and that bound is the backpressure policy: a full queue means this
    subscriber cannot keep up, and the answer is to drop *it* rather than to let it delay a pass
    (RC-6). The resulting `seq` gap is visible to the consumer and recoverable via `?since=` — which
    is strictly better than the alternative the shared-lock era would have produced, where a slow
    reader could hold up the producer (the ISSUE_73 failure class).

    `dropped` is read by the connection's own send loop, which is what closes the socket. The
    dispatcher never touches the transport.
    """
    pipeline_id: str
    queue: 'asyncio.Queue[Dict[str, Any]]'
    dropped: bool = False
