"""The viewer's verdict about itself (ISSUE_126 Phase 2).

Everything on that screen was measured on another machine, so the moment the connection drops every
number describes the past while looking exactly as current as it did a second earlier. These cases
are the four properties that keep it honest, all four borrowed from the FiniexDataCollector's §5
because they had already paid for them.
"""
from datetime import datetime, timedelta, timezone

from rich.console import Console

from finiexragengine.core.ui.dashboard_feed import DashboardFeed, FeedReading
from finiexragengine.core.ui.dashboard_viewer import DashboardViewer
from finiexragengine.core.ui.remote_engine_state import RemoteEngineState

_SNAPSHOT_AT = datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)

_PAYLOAD = {
    'view': 'engine', 'version': '0.3.3',
    'snapshot_at': '2026-09-22T13:30:00Z',
    'engine_started_at': '2026-09-22T11:30:00Z',
    'journal_named': True,
    'state': {
        'breaking': {'last': None, 'detected': 0, 'confirmed': 0, 'detail': '', 'by_trigger': {}},
        'sources': {'crypto_news': {'last': '2026-09-22T13:29:00Z', 'ok': 11, 'total': 11,
                                    'deviations': [], 'host_backoff_until': None,
                                    'host_detail': ''}},
        'stalled': [],
    },
}


def _viewer() -> DashboardViewer:
    feed = DashboardFeed('https://engine.test', 'a-token-that-is-not-a-real-credential')
    return DashboardViewer(feed, console=Console(record=True, width=120, height=42))


def _draw(viewer: DashboardViewer, now: datetime) -> str:
    console = Console(record=True, width=120, height=42)
    console.print(viewer.render(now=now))
    return console.export_text()


def _good(at: datetime) -> FeedReading:
    return FeedReading(at=at, state=RemoteEngineState.from_payload(_PAYLOAD))


def test_before_the_first_reading_it_says_so_rather_than_inventing_a_time() -> None:
    """`never answered` — a viewer that shows an age it does not have is the defect in miniature."""
    panel = _draw(_viewer(), _SNAPSHOT_AT)

    assert 'never answered' in panel


def test_a_failure_keeps_the_last_numbers_and_states_their_age() -> None:
    """Blanking them throws away what the engine last said, which is the interesting part.

    So the panel stays, and what changes is the frame around it: the clock time of the last good
    reading, how long ago that was, and the sentence saying why nothing has arrived since.
    """
    viewer = _viewer()
    viewer.take(_good(_SNAPSHOT_AT))
    viewer.take(FeedReading(at=_SNAPSHOT_AT + timedelta(minutes=1),
                            reason='connection refused — the engine is not running'))

    panel = _draw(viewer, _SNAPSHOT_AT + timedelta(minutes=2, seconds=17))

    assert '11/11 ok' in panel, 'the last numbers must survive the outage'
    assert 'no answer since 13:30:00 UTC' in panel
    assert '(2m' in panel, 'the age of the last good reading belongs on screen'
    assert 'not running' in panel


def test_the_age_keeps_moving_while_nothing_arrives() -> None:
    """The panel is frozen by design — every age in it is measured against the engine's stamp.

    So the age of the READING is the one number that must keep counting, or a dead feed looks
    exactly like a quiet engine.
    """
    viewer = _viewer()
    viewer.take(_good(_SNAPSHOT_AT))
    viewer.take(FeedReading(at=_SNAPSHOT_AT, reason='timed out'))

    early = _draw(viewer, _SNAPSHOT_AT + timedelta(seconds=30))
    later = _draw(viewer, _SNAPSHOT_AT + timedelta(minutes=9))

    assert early != later


def test_two_clocks_that_disagree_are_named_not_absorbed() -> None:
    """Uptime and every age come from the producer's clock; a viewer minutes off would print a
    panel that is internally consistent and wrong about when any of it happened."""
    aligned = _viewer()
    aligned.take(_good(_SNAPSHOT_AT + timedelta(seconds=1)))
    assert 'clocks differ' not in _draw(aligned, _SNAPSHOT_AT + timedelta(seconds=2))

    skewed = _viewer()
    skewed.take(_good(_SNAPSHOT_AT + timedelta(seconds=40)))
    assert 'clocks differ by +40s' in _draw(skewed, _SNAPSHOT_AT + timedelta(seconds=41))


def test_the_panel_is_drawn_in_the_producers_clock() -> None:
    """The uptime is the engine's, not this process's — 11:30 to 13:30 is two hours."""
    viewer = _viewer()
    viewer.take(_good(_SNAPSHOT_AT))

    panel = _draw(viewer, _SNAPSHOT_AT + timedelta(seconds=3))

    assert 'up 2h' in panel
    # 13:29:00 against the engine's 13:30:00 stamp is 60s — and against OUR clock three
    # seconds later it would be 63s. The distinction is the test.
    assert 'last 60s' in panel, 'the source row ages against the engine stamp, not ours'
