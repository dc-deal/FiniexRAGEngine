"""SourceReach — config ∩ health, the one definition of the envelope's reach numbers.

Pure logic: a fake health store, so no DB and no API budget are touched.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, Set

from finiexragengine.core.observability.source_reach import SourceReach
from finiexragengine.types.config_types.source_set_types import SourceConfig, SourceSetConfig
from finiexragengine.types.ingest_types import SourceHealthState

_NOW = datetime.now(timezone.utc)


class _FakeHealthStore:
    """Stands in for SourceHealthStore.states_of — fixed rows, no DB."""

    def __init__(self, states: Dict[str, SourceHealthState]) -> None:
        self._states = states
        self.asked_for: Set[str] = set()

    def states_of(self, source_ids: Set[str]) -> Dict[str, SourceHealthState]:
        self.asked_for = set(source_ids)
        return {sid: state for sid, state in self._states.items() if sid in source_ids}


def _healthy(source_id: str) -> SourceHealthState:
    return SourceHealthState(source_id=source_id, consecutive_failures=0)


def _source_set() -> SourceSetConfig:
    return SourceSetConfig(source_set_id='forex_news', sources=[
        SourceConfig(source_id='fxstreet', url='https://fx.test/rss', enabled=False),
        SourceConfig(source_id='forexlive', url='https://fl.test/rss'),
        SourceConfig(source_id='boe_news', url='https://boe.test/rss'),
    ])


def test_disabled_source_is_in_neither_number():
    # Switching a feed off is a decision, not a degradation: it must not be reported as a source
    # the run failed to reach. Counting it would claim a contribution that does not exist.
    health = _FakeHealthStore({'forexlive': _healthy('forexlive'), 'boe_news': _healthy('boe_news')})
    census = SourceReach(_source_set(), health).census()

    assert census.configured == 2                 # the two enabled feeds — not the catalogue's 3
    assert census.reached == 2
    assert census.unreached == []
    assert 'fxstreet' not in health.asked_for     # health is never even asked about it


def test_a_quarantined_source_is_subtracted_and_says_how_long():
    # The case that was invisible before: boe_news is in cool-off, so it never failed a fetch this
    # pass — the old `configured - failed` arithmetic therefore counted it as reached.
    health = _FakeHealthStore({
        'forexlive': _healthy('forexlive'),
        'boe_news': SourceHealthState('boe_news', consecutive_failures=5,
                                      quarantined_until=_NOW + timedelta(hours=3),
                                      last_error_type='HTTP_ERROR', last_status=403),
    })
    census = SourceReach(_source_set(), health).census()

    assert census.configured == 2 and census.reached == 1
    assert [u.source_id for u in census.unreached] == ['boe_news']
    reason = census.unreached[0].reason
    assert 'quarantined until' in reason and '5 consecutive failures' in reason
    assert 'HTTP_ERROR 403' in reason              # the cause travels into the envelope


def test_an_elapsed_quarantine_reads_as_failing_not_as_held():
    # A cool-off that ran out keeps its `quarantined_until` in the row until a successful poll
    # clears it. Testing the column for presence alone would report a date in the *past* as if the
    # feed were still held back — it is retryable and simply has not succeeded yet.
    health = _FakeHealthStore({
        'forexlive': _healthy('forexlive'),
        'boe_news': SourceHealthState('boe_news', consecutive_failures=5,
                                      quarantined_until=_NOW - timedelta(hours=1),
                                      last_error_type='HTTP_ERROR', last_status=403),
    })
    census = SourceReach(_source_set(), health).census()

    assert census.reached == 1                              # still not delivering
    assert 'quarantined' not in census.unreached[0].reason  # ... but not for that reason
    assert 'last poll failed' in census.unreached[0].reason


def test_a_set_whose_feeds_never_polled_reports_zero_reach():
    # A fresh database has no health rows at all. Reporting full reach here would be the original
    # bug: claiming every feed delivered before a single one was ever polled.
    census = SourceReach(_source_set(), _FakeHealthStore({})).census()

    assert census.configured == 2 and census.reached == 0
    assert [u.source_id for u in census.unreached] == ['forexlive', 'boe_news']   # config order
    assert all(u.reason == 'never polled' for u in census.unreached)


def test_a_set_with_every_source_disabled_is_zero_of_zero():
    # Degenerate but real (an environment that switched everything off): no division, no crash,
    # and 0/0 is honest — nothing is configured to run, so nothing was missed.
    source_set = SourceSetConfig(source_set_id='forex_news', sources=[
        SourceConfig(source_id='fxstreet', url='https://fx.test/rss', enabled=False),
    ])
    census = SourceReach(source_set, _FakeHealthStore({})).census()

    assert census.configured == 0 and census.reached == 0
    assert census.unreached == []
