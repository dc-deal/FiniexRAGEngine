"""The viewer's half of the wire (ISSUE_126 Phase 2) — a reading, or a reason in words.

The subject is the *reason*. A viewer that reports "error" sends an operator nowhere; the four
common failures here live on four different machines, and the sentence is what separates them. The
timeout floor is measured rather than chosen: a refused TCP connection took 2.04 s to report itself
on the engine's Windows host, so a 2.0 s timeout turned "not running" into "not answering" — the
wrong sentence, by forty milliseconds.
"""
import httpx
import pytest

from finiexragengine.core.ui.dashboard_feed import DashboardFeed


def _feed(**kwargs) -> DashboardFeed:
    return DashboardFeed('https://engine.test', 'a-token-that-is-not-a-real-credential', **kwargs)


def _answer(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    def replace(*args: object, **kwargs: object) -> object:
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(httpx, 'get', replace)


@pytest.mark.parametrize('failure, expected', [
    (httpx.ConnectError('refused'), 'not running'),
    (httpx.ReadTimeout('slow'), 'not answering'),
])
def test_a_transport_failure_says_which_machine_to_go_to(
        monkeypatch: pytest.MonkeyPatch, failure: Exception, expected: str) -> None:
    """Refused and timed out are opposite diagnoses: one process is absent, the other is stuck."""
    _answer(monkeypatch, failure)

    reading = _feed().poll()

    assert not reading.ok
    assert expected in reading.reason


@pytest.mark.parametrize('status, expected', [
    (401, 'credential is wrong'),
    (403, 'grant for this view is missing'),
    (404, 'no such view'),
    (503, 'collecting'),
])
def test_each_refusal_is_a_sentence_rather_than_a_number(
        monkeypatch: pytest.MonkeyPatch, status: int, expected: str) -> None:
    """401 and 403 differ by who must fix it; 503 is the engine answering honestly about itself."""
    _answer(monkeypatch, httpx.Response(status, json={'detail': 'nothing is collecting'}))

    reading = _feed().poll()

    assert not reading.ok
    assert expected in reading.reason


def test_the_timeout_never_drops_below_the_measured_floor() -> None:
    """Asked for 2.0 s, it takes 5 — because 2.04 s is how long a refusal needs to arrive."""
    assert _feed(timeout_seconds=2.0)._timeout == 5.0
    assert _feed(timeout_seconds=30.0)._timeout == 30.0


def test_a_payload_this_viewer_cannot_rebuild_is_a_stated_condition(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The likeliest cause is an engine older than this checkout — a sentence, not a traceback."""
    _answer(monkeypatch, httpx.Response(200, json={'state': {'breaking': {}}}))

    reading = _feed().poll()

    assert not reading.ok
    assert 'unreadable payload' in reading.reason


def test_a_good_answer_arrives_as_the_shape_the_renderer_reads(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: what comes back is readable exactly where `EngineStats` is."""
    _answer(monkeypatch, httpx.Response(200, json={
        'view': 'engine', 'version': '0.3.3', 'snapshot_at': '2026-09-22T13:30:00Z',
        'engine_started_at': '2026-09-22T11:30:00Z', 'journal_named': True,
        'state': {'breaking': {'last': None, 'detected': 0, 'confirmed': 0,
                               'detail': '', 'by_trigger': {}},
                  'sources': {'crypto_news': None}, 'stalled': []}}))

    reading = _feed().poll()

    assert reading.ok
    assert reading.state.version() == '0.3.3'
    assert reading.state.sources() == {'crypto_news': None}
    assert reading.state.watchdog().stalled_workers() == set()
