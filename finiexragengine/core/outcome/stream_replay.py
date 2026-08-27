"""What a connect resolves to: the replay policy behind `?history=N` and `?since=&epoch=` (ISSUE_9).

**One replay path, three entry points** — the stream's connect snapshot, the stream's reconnect
resync, and the range endpoint a collector catches up with. They share this unit rather than each
deriving the same three boundary conditions, because the conditions are where the disagreements
would live: whether a cursor is too old, too new, or from a series that no longer exists.

The unit answers with a `ReplayPlan` and writes nothing. That is what lets the same policy serve SSE
frames and plain JSON: the stream renders the plan as `control` frames plus `signal` frames, the
range endpoint renders it as a body plus an HTTP status. Two renderings, one decision.

The three conditions, and why two of them end the connection:

* **`replay_truncated`** — the cursor is older than the window. Recoverable *and* useful: the marker
  names the oldest `seq` we still hold and the replay proceeds from there, so the consumer knows
  exactly what it lost instead of receiving a silent partial fill.
* **`cursor_ahead`** — the cursor is beyond our head, which happens after a *consumer-side* store
  restore. Nothing can be replayed, and falling through to live would hand them frames below a mark
  they believe they have passed.
* **`epoch_changed`** — the series was rewound on *our* side, so their cursor addresses numbers that
  now mean something else. Serving `since+1..` would be the worst possible answer: numbers they
  believe they have already seen, carrying content they never have.

The last two are **terminal** (emit, then close). Same remedy — reconnect — but deliberately
different diagnoses: `epoch_changed` means we rewound, `cursor_ahead` means they did, and an operator
needs to be alerted differently.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.types.stream_types import ControlFrame, ReplayPlan, StreamHead

logger = logging.getLogger(__name__)


class StreamReplay:
    """Resolves a connect request into a `ReplayPlan`. Reads the store; writes nothing."""

    def __init__(self, store: OutcomeStore, replay_window_hours: int,
                 max_replay_frames: int = 500) -> None:
        self._store = store
        self._window_hours = replay_window_hours
        # The window bounds AGE; this bounds VOLUME. They are not redundant — a window that holds
        # nothing clamps nothing, and a cursor far in the past would then replay the whole tail.
        self._max_frames = max_replay_frames

    def plan(self, pipeline_id: str, history: Optional[int] = None,
             since: Optional[int] = None, epoch: Optional[int] = None,
             limit: Optional[int] = None) -> ReplayPlan:
        """Resolve one connect. `history` and `since` are mutually exclusive — the caller enforces it.

        Passing neither is "live only": a plan carrying no envelopes and `control`/`live`, which is
        emitted anyway so *"the replay ended"* is never left to be inferred from a pause.

        `limit` caps the cursor path for a **paging** caller (the range endpoint). The stream never
        passes it: a replay that stopped short would leave the connection live with a hole in front
        of it, which is the one shape the gapless promise cannot absorb.
        """
        head = self._store.stream_head(pipeline_id)
        if since is not None:
            return self._from_cursor(pipeline_id, head, since, epoch, limit)
        if history:
            return ReplayPlan(head=head,
                              envelopes=self._recent(pipeline_id, head.seq, history))
        return ReplayPlan(head=head)

    # --- the cursor path ------------------------------------------------------------------------

    def _from_cursor(self, pipeline_id: str, head: StreamHead, since: int,
                     epoch: Optional[int], limit: Optional[int] = None) -> ReplayPlan:
        # An epoch of 0 means the sequencer has no counter row for this stream, so there is no series
        # to disagree about — not a mismatch. In production `reconcile()` seeds every registered
        # stream at boot, so this is the narrow window before that has run (or a stream added at
        # runtime), and treating it as a rewind would answer a resync to a consumer who is simply
        # early.
        if epoch is not None and head.epoch and epoch != head.epoch:
            logger.info('[STREAM] %s: cursor epoch %d != current %d — epoch_changed (terminal)',
                        pipeline_id, epoch, head.epoch)
            return ReplayPlan(
                head=head, emit_live=False, terminal=True,
                control=ControlFrame('epoch_changed', {
                    # `stream_epoch` on the frame is the NEW epoch (the rule has no per-frame
                    # exception); `previous_epoch` is the one the recipient was on.
                    'previous_epoch': epoch, 'head_seq': head.seq}))

        if since > head.seq:
            logger.info('[STREAM] %s: cursor %d ahead of head %d — cursor_ahead (terminal)',
                        pipeline_id, since, head.seq)
            return ReplayPlan(
                head=head, emit_live=False, terminal=True,
                control=ControlFrame('cursor_ahead', {
                    'requested_since': since, 'head_seq': head.seq}))

        # Two independent floors, and the caller is told about whichever bites harder. The age floor
        # is the window; the volume floor is `max_replay_frames` measured back from the head. Taking
        # the maximum means a replay is bounded even when the window holds nothing at all.
        requested = since
        age_floor = self._window_floor(pipeline_id)
        volume_floor = max(1, head.seq - self._max_frames + 1)
        floor = volume_floor if age_floor is None else max(age_floor, volume_floor)
        control = None
        if since < floor - 1:
            # Truncation is explicit and names the oldest position we will replay. A silent partial
            # fill would leave the consumer believing the gap between their cursor and that position
            # was never produced — where the marker sends them to the journal export for it (#62).
            since = floor - 1
            control = ControlFrame('replay_truncated', {
                'requested_since': requested, 'oldest_available_seq': floor,
                'window_hours': self._window_hours})

        bound = self._limit(head.seq, since)
        envelopes = self._store.envelopes_by_seq(
            pipeline_id, after_seq=since,
            limit=bound if limit is None else min(bound, limit))
        return ReplayPlan(head=head, control=control, envelopes=envelopes)

    # --- the history path -----------------------------------------------------------------------

    def _recent(self, pipeline_id: str, head_seq: int,
                history: int) -> List[Dict[str, Any]]:
        """The last `history` envelopes, bounded by the window — **except the newest one**.

        The snapshot is never suppressed by age, and that is a decision rather than an oversight.
        `history=1` *is* the connect snapshot: its job is to state the current state, so withholding
        it on a stream whose last pass predates the window would make a quiet producer
        indistinguishable from a cold one — and "cold" is a claim (`head_seq: 0`) that would then be
        false. Additional history frames beyond the newest are bounded normally.
        """
        if head_seq <= 0:
            return []                                  # cold stream: nothing to snapshot
        floor = self._window_floor(pipeline_id)
        # A window holding nothing still owes a snapshot, so the newest position is the floor there
        # — otherwise a quiet stream would return no frames at all while claiming a non-zero head.
        oldest_allowed = head_seq if floor is None else floor
        after = max(head_seq - history, oldest_allowed - 1,
                    head_seq - self._max_frames)          # the volume bound, always
        return self._store.envelopes_by_seq(pipeline_id, after_seq=after,
                                            limit=head_seq - after)

    # --- bounds ---------------------------------------------------------------------------------

    def _window_floor(self, pipeline_id: str) -> Optional[int]:
        """The oldest `seq` inside `replay_window_hours`; None when the window holds nothing."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._window_hours)
        return self._store.oldest_seq_since(pipeline_id, cutoff)

    @staticmethod
    def _limit(head_seq: int, after_seq: int) -> int:
        """How many rows a replay may fetch — the distance to the head, never an open range.

        Derived rather than configured: the window already bounds how far back a cursor may point,
        so a second knob could only disagree with it. At least 1, so a cursor sitting exactly on the
        head asks a bounded question instead of `LIMIT 0`.
        """
        return max(1, head_seq - after_seq)
