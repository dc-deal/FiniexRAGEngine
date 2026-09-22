"""The live state as one transportable reading (ISSUE_126 Phase 2).

The subject here is not the numbers — `test_engine_stats.py` owns those — but the two properties
that only matter once the reading crosses a wire:

**A verdict travels, never the material to re-compute it.** `over_ceiling` is decided against
engine-local configuration and `stalled` against each worker's own cadence. A viewer re-deriving
either from its own config is the defect the FiniexDataCollector paid for, when a renderer read a
*local* file-size limit and drew a remote file of 12,737 ticks as 1274 % of a boundary that instance
does not have.

**An unknown is a third state.** In one process an absent collaborator is genuinely absent and the
renderer's benign default is right. Across a wire the same absence means "the engine did not tell
us", and a screen that draws that as all-clear is a screen that lies. So `None` and `[]` must not
collapse into each other anywhere in this payload.
"""
from datetime import datetime, timezone

from finiexragengine.core.ui.dashboard_snapshot import (
    DASHBOARD_VIEWS,
    sample_dashboard,
)
from finiexragengine.core.ui.engine_stats import EngineStats
from finiexragengine.types.worker_types import WorkerState
from finiexragengine.utils.dataclass_json import to_jsonable


class _Watchdog:
    """Only the method the sampler calls — the point is the set, not the watchdog."""

    def __init__(self, stalled: set) -> None:
        self._stalled = stalled

    def stalled_workers(self) -> set:
        return self._stalled


class _Gauge:
    def __init__(self, status: dict) -> None:
        self._status = status

    def status(self) -> dict:
        return self._status


def _stats() -> EngineStats:
    return EngineStats(source_set_ids=['crypto_news'], pipeline_ids=['crypto_sentiment'])


def test_an_absent_collaborator_is_null_and_an_empty_one_is_empty() -> None:
    """`None` is "the engine has no watchdog"; `[]` is "it ran and found nothing".

    Collapsing the two is how a viewer paints every row healthy because nobody was watching. The
    renderer's in-process default — an empty set when no watchdog was supplied — is correct there
    and would be a lie here, which is why the distinction is made at the sampler rather than left
    to whoever draws it.
    """
    without = sample_dashboard(_stats())
    assert without.state.stalled is None
    assert without.state.budget is None
    assert without.state.resources is None
    assert without.state.workers is None

    with_watchdog = sample_dashboard(_stats(), stall_watchdog=_Watchdog(set()))
    assert with_watchdog.state.stalled == []


def test_the_engines_verdict_travels_not_the_threshold_behind_it() -> None:
    """`over_ceiling` is computed against engine-local config, so the answer crosses, not the inputs.

    The gauge's own `status()` is the source — the same one `/v1/health` serves — so the two
    surfaces cannot drift apart about whether this process is over its ceiling.
    """
    gauge = _Gauge({'enabled': True, 'rss_mb': 812.5, 'open_sockets': 9, 'threads': 21,
                    'sampled_at': '2026-09-22T11:00:00Z', 'ceiling_mb': 700, 'over_ceiling': True})
    snapshot = sample_dashboard(_stats(), resource_gauge=gauge)

    assert snapshot.state.resources is not None
    assert snapshot.state.resources.over_ceiling is True
    assert snapshot.state.resources.ceiling_mb == 700
    assert snapshot.state.resources.rss_mb == 812.5


def test_a_stalled_set_is_sorted_so_two_identical_readings_serialize_identically() -> None:
    """An unordered set would turn a diff between two fetches into noise."""
    watchdog = _Watchdog({'eval:crypto_sentiment', 'ingest:crypto_news'})
    assert sample_dashboard(_stats(), stall_watchdog=watchdog).state.stalled == [
        'eval:crypto_sentiment', 'ingest:crypto_news']


def test_only_the_two_fields_the_header_reads_travel_per_worker() -> None:
    """`WorkerState` carries more; promising all of it would make the rest a contract by accident."""
    state = WorkerState(name='ingest:crypto_news', kind='ingest', interval_seconds=15)
    state.runs = 41
    state.stopped_at = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
    state.stopped_reason = 'RuntimeError("boom")'

    workers = sample_dashboard(_stats(), states_provider=lambda: [state]).state.workers

    assert workers is not None and len(workers) == 1
    assert workers[0].name == 'ingest:crypto_news'
    assert workers[0].stopped_reason == 'RuntimeError("boom")'
    assert not hasattr(workers[0], 'runs')


def test_the_reading_is_stamped_by_the_engines_clock() -> None:
    """Without `snapshot_at` a frozen fetch is indistinguishable from a live one.

    It is also what every age on the screen must be computed against: otherwise each one subtracts
    an engine timestamp from the viewer's clock, and the skew hides inside the number.
    """
    before = datetime.now(timezone.utc)
    snapshot = sample_dashboard(_stats(), version='0.3.3')
    assert before <= snapshot.snapshot_at <= datetime.now(timezone.utc)
    assert snapshot.snapshot_at.tzinfo is not None
    assert snapshot.view in DASHBOARD_VIEWS


def test_journal_named_is_tri_state() -> None:
    """Named, unnamed, and never established are three different facts about a producer."""
    assert sample_dashboard(_stats()).journal_named is None
    assert sample_dashboard(_stats(), journal_named=False).journal_named is False
    assert sample_dashboard(_stats(), journal_named=True).journal_named is True


def test_the_payload_converts_structurally_so_a_new_measurement_needs_no_converter_edit() -> None:
    """Fields are walked, never enumerated — and datetimes come out as UTC with a `Z`.

    The round trip through `to_jsonable` is the wire format, so anything it refuses would be a
    payload that cannot be served; asserting it here is cheaper than finding out from a 500.
    """
    stats = _stats()
    stats.push_event('INGEST', 'fetched 162 · stored 3')
    stats.add_breaking_detected(2, at=datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc),
                                by_trigger={'keyword': 2})

    payload = to_jsonable(sample_dashboard(stats, version='0.3.3').state)

    assert payload['events'][0]['stage'] == 'INGEST'
    assert payload['events'][0]['ts'].endswith('Z')
    assert payload['breaking']['detected'] == 2
    assert payload['breaking']['by_trigger'] == {'keyword': 2}
    # Pre-registered keys survive as explicit nulls rather than vanishing: a missing row and a row
    # that has not run yet are different, and the panel draws them differently.
    assert payload['sources'] == {'crypto_news': None}
