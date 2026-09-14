"""The replay policy (ISSUE_9 §3.3) — needs a reachable Postgres (skipped otherwise), no budget.

One decision unit behind three entry points: the stream's connect snapshot, its reconnect resync,
and the range endpoint. What is tested here is the *policy*, not the transport — a plan is a value,
so every boundary condition is assertable without a socket.
"""
from datetime import datetime, timedelta, timezone

import pytest

from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.outcome.stream_replay import StreamReplay
from finiexragengine.types.outcome_types import RunMetadata, SentimentEnvelope, SentimentResult

_NOW = datetime.now(timezone.utc)


def _envelope(pipeline_id: str = 'p', ts: datetime = _NOW) -> SentimentEnvelope:
    return SentimentEnvelope(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed', prompt_version='4',
        timestamp=ts, status='success',
        result=[SentimentResult(symbol='BTCUSD', signal='BUY', sentiment_score=0.4,
                                confidence=0.8, reasoning='bullish')],
        metadata=RunMetadata(model='gpt-4o-mini'))


@pytest.fixture
def store(clean_db: str) -> OutcomeStore:
    return OutcomeStore(clean_db)


@pytest.fixture
def replay(store: OutcomeStore) -> StreamReplay:
    return StreamReplay(store, replay_window_hours=24)


def _seed(store: OutcomeStore, count: int, ts: datetime = _NOW) -> None:
    for _ in range(count):
        store.save(_envelope(ts=ts))


# --- the history path ---------------------------------------------------------------------------

def test_the_default_history_is_the_connect_snapshot(store, replay):
    _seed(store, 3)

    plan = replay.plan('p', history=1)

    assert [env['seq'] for env in plan.envelopes] == [3]
    assert plan.emit_live is True and plan.terminal is False


def test_history_returns_the_last_n_ascending(store, replay):
    _seed(store, 5)

    plan = replay.plan('p', history=3)

    assert [env['seq'] for env in plan.envelopes] == [3, 4, 5]


def test_a_cold_stream_has_no_snapshot_frame_and_says_so_with_head_zero(store, replay):
    """No snapshot because there is nothing to snapshot; `head_seq: 0` is the claim, and `seq: 0`
    can never collide with a real position because the counter returns seq+1."""
    plan = replay.plan('p', history=1)

    assert plan.envelopes == []
    assert plan.head.seq == 0
    assert plan.emit_live is True


def test_the_snapshot_survives_a_window_that_holds_nothing(store, replay):
    """A stream whose last pass predates the window is QUIET, not cold — and `head_seq` would
    otherwise report a position whose envelope the connect refused to send."""
    _seed(store, 2, ts=_NOW - timedelta(days=3))

    plan = replay.plan('p', history=5)

    assert [env['seq'] for env in plan.envelopes] == [2]        # the newest, despite its age
    assert plan.control is None


def test_history_beyond_the_newest_is_bounded_by_the_window(store, replay):
    """The newest is exempt from the age bound; everything older than the window is not."""
    _seed(store, 2, ts=_NOW - timedelta(days=3))               # seq 1, 2 — outside
    _seed(store, 2, ts=_NOW)                                   # seq 3, 4 — inside

    plan = replay.plan('p', history=10)

    assert [env['seq'] for env in plan.envelopes] == [3, 4]


# --- the cursor path ---------------------------------------------------------------------------

def test_a_cursor_replays_strictly_after_itself(store, replay):
    _seed(store, 4)

    plan = replay.plan('p', since=2, epoch=1)

    assert [env['seq'] for env in plan.envelopes] == [3, 4]
    assert plan.control is None                                # the snapshot is suppressed


def test_a_cursor_on_the_head_replays_nothing_and_goes_live(store, replay):
    _seed(store, 2)

    plan = replay.plan('p', since=2, epoch=1)

    assert plan.envelopes == []
    assert plan.emit_live is True


def test_a_cursor_older_than_the_window_is_truncated_explicitly(store, replay):
    """Never a silent partial fill: the marker names the oldest position still held, and the replay
    proceeds from there, so the consumer knows exactly what it lost."""
    _seed(store, 2, ts=_NOW - timedelta(days=3))               # seq 1, 2 — outside
    _seed(store, 2, ts=_NOW)                                   # seq 3, 4 — inside

    plan = replay.plan('p', since=0, epoch=1)

    assert plan.control is not None
    assert plan.control.code == 'replay_truncated'
    assert plan.control.fields == {'requested_since': 0, 'oldest_available_seq': 3,
                                   'window_hours': 24}
    assert [env['seq'] for env in plan.envelopes] == [3, 4]     # replay continues
    assert plan.terminal is False


def test_a_cursor_ahead_of_the_head_is_terminal_and_replays_nothing(store, replay):
    """A consumer-side store restore. Falling through to live would hand them frames below a mark
    they believe they have passed."""
    _seed(store, 2)

    plan = replay.plan('p', since=9001, epoch=1)

    assert plan.control.code == 'cursor_ahead'
    assert plan.control.fields == {'requested_since': 9001, 'head_seq': 2}
    assert plan.envelopes == []
    assert plan.terminal is True and plan.emit_live is False


def test_an_epoch_mismatch_is_terminal_and_carries_both_epochs(store, replay):
    """Serving `since+1..` of a different series is the worst answer available: numbers they believe
    they have seen, carrying content they never have."""
    _seed(store, 3)

    plan = replay.plan('p', since=1, epoch=7)

    assert plan.control.code == 'epoch_changed'
    assert plan.control.fields == {'previous_epoch': 7, 'head_seq': 3}
    assert plan.head.epoch == 1                                # the NEW epoch, for the frame
    assert plan.envelopes == []
    assert plan.terminal is True


def test_the_epoch_check_is_skipped_when_no_counter_row_exists_yet(store, replay):
    """`epoch: 0` means the sequencer has no row for this stream — there is no series to disagree
    about, so answering a resync would refuse a consumer who is merely early."""
    plan = replay.plan('p', since=0, epoch=1)

    assert plan.control is None
    assert plan.terminal is False


def test_neither_history_nor_cursor_is_live_only(store, replay):
    """`control`/`live` is emitted anyway, so "the replay ended" is never inferred from a pause."""
    _seed(store, 2)

    plan = replay.plan('p')

    assert plan.envelopes == []
    assert plan.emit_live is True


def test_a_replay_never_crosses_into_another_stream(store, replay):
    store.save(_envelope(pipeline_id='p'))
    store.save(_envelope(pipeline_id='other'))
    store.save(_envelope(pipeline_id='p'))

    plan = replay.plan('p', since=0, epoch=1)

    assert [env['pipeline_id'] for env in plan.envelopes] == ['p', 'p']


# --- the volume bound, which the window cannot provide -------------------------------------------

def test_a_replay_is_bounded_even_when_the_window_holds_nothing(store, clean_db):
    """Found on the wire, not here: the age floor clamps nothing when the window is empty.

    A stream whose last pass predates `replay_window_hours` has no age floor at all, so a cursor far
    in the past replayed the WHOLE tail in one burst — 164 envelopes at ~34 kB on the dev journal,
    and the same shape orders of magnitude worse on a production-length series. The window bounds
    age; this bounds volume, and the two are independent.
    """
    replay = StreamReplay(store, replay_window_hours=24, max_replay_frames=3)
    for _ in range(10):
        store.save(_envelope(ts=_NOW - timedelta(days=3)))     # every one outside the window

    plan = replay.plan('p', since=0, epoch=1)

    assert [env['seq'] for env in plan.envelopes] == [8, 9, 10]
    assert plan.control.code == 'replay_truncated'
    assert plan.control.fields['requested_since'] == 0
    assert plan.control.fields['oldest_available_seq'] == 8        # where the replay really starts


def test_the_harder_of_the_two_floors_wins(store):
    """Both floors are reported through one marker, because the consumer's remedy is the same: fetch
    the span between `requested_since` and `oldest_available_seq` from the journal export."""
    replay = StreamReplay(store, replay_window_hours=24, max_replay_frames=2)
    store.save(_envelope(ts=_NOW - timedelta(days=3)))         # seq 1 — outside the window
    for _ in range(4):
        store.save(_envelope())                                # seq 2..5 — inside

    plan = replay.plan('p', since=0, epoch=1)

    # The age floor is 2; the volume floor is 5 - 2 + 1 = 4. The volume floor bites harder.
    assert [env['seq'] for env in plan.envelopes] == [4, 5]
    assert plan.control.fields['oldest_available_seq'] == 4


def test_history_is_capped_by_the_volume_bound_too(store):
    """`history=N` is a caller-supplied number, so it needs the same ceiling as a cursor."""
    replay = StreamReplay(store, replay_window_hours=24, max_replay_frames=2)
    for _ in range(6):
        store.save(_envelope())

    plan = replay.plan('p', history=100)

    assert [env['seq'] for env in plan.envelopes] == [5, 6]
