"""SourceReach — config ∩ health, the one definition of the envelope's reach numbers.

Pure logic: a fake health store, so no DB and no API budget are touched.
"""
from typing import Set

from finiexragengine.core.observability.source_reach import SourceReach
from finiexragengine.types.config_types.source_set_types import SourceConfig, SourceSetConfig


class _FakeHealthStore:
    """Stands in for SourceHealthStore.reach_of — a fixed set of delivering feeds."""

    def __init__(self, delivering: Set[str]) -> None:
        self._delivering = delivering
        self.asked_for: Set[str] = set()

    def reach_of(self, source_ids: Set[str]) -> Set[str]:
        self.asked_for = set(source_ids)
        return self._delivering & set(source_ids)


def _source_set() -> SourceSetConfig:
    return SourceSetConfig(source_set_id='forex_news', sources=[
        SourceConfig(source_id='fxstreet', url='https://fx.test/rss', enabled=False),
        SourceConfig(source_id='forexlive', url='https://fl.test/rss'),
        SourceConfig(source_id='boe_news', url='https://boe.test/rss'),
    ])


def test_disabled_source_is_in_neither_number():
    # Switching a feed off is a decision, not a degradation: it must not be reported as a source
    # the run failed to reach. Counting it would claim a contribution that never existed.
    health = _FakeHealthStore({'forexlive', 'boe_news'})
    census = SourceReach(_source_set(), health).census()

    assert census.configured == 2                 # the two enabled feeds — not the catalogue's 3
    assert census.reached == 2
    assert census.unreached == []
    assert 'fxstreet' not in health.asked_for     # health is never even asked about it


def test_a_source_not_delivering_is_subtracted_and_named():
    # The case that was invisible before: boe_news is in cool-off, so it never failed a fetch
    # this pass — the old `configured - failed` arithmetic therefore counted it as reached.
    health = _FakeHealthStore({'forexlive'})
    census = SourceReach(_source_set(), health).census()

    assert census.configured == 2
    assert census.reached == 1
    assert census.unreached == ['boe_news']       # named, so the gap is debuggable


def test_a_set_whose_feeds_never_polled_reports_zero_reach():
    # A fresh database has no health rows at all. Reporting full reach here would be the original
    # bug: claiming every feed delivered before a single one was ever polled.
    census = SourceReach(_source_set(), _FakeHealthStore(set())).census()

    assert census.configured == 2
    assert census.reached == 0
    assert census.unreached == ['forexlive', 'boe_news']


def test_unreached_keeps_the_declared_order():
    # A stable list reads the same run to run — a set that reorders per call is noise in a diff.
    health = _FakeHealthStore(set())
    census = SourceReach(_source_set(), health).census()
    assert census.unreached == ['forexlive', 'boe_news']   # config order, not set iteration order


def test_a_set_with_every_source_disabled_is_zero_of_zero():
    # Degenerate but real (an environment that switched everything off): no division, no crash,
    # and 0/0 is honest — nothing is configured to run, so nothing was missed.
    source_set = SourceSetConfig(source_set_id='forex_news', sources=[
        SourceConfig(source_id='fxstreet', url='https://fx.test/rss', enabled=False),
    ])
    census = SourceReach(source_set, _FakeHealthStore(set())).census()

    assert census.configured == 0 and census.reached == 0
    assert census.unreached == []
