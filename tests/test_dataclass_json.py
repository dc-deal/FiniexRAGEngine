"""Report serialization (ISSUE_104) — what reaches the wire, and what must not.

Two failure modes shape this unit and therefore these tests: a payload that silently *loses* the
derived values the console shows, and a payload that silently *gains* an engine object's private
state because a generic encoder fell back to `vars()`. Both are quiet, so both are pinned here.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pytest

from finiexragengine.utils.dataclass_json import to_jsonable


@dataclass
class _Row:
    name: str
    hits: int
    total: int
    when: Optional[datetime] = None

    @property
    def rate(self) -> float:
        """A derived value of exactly the kind the reports keep in properties."""
        return self.hits / self.total if self.total else 0.0

    @property
    def _hidden(self) -> str:
        return 'private properties stay out'


@dataclass
class _Report:
    rows: List[_Row] = field(default_factory=list)
    labels: Dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.rows)


class _Policy:
    """A behaviour object: it has state, and only some of it explains a report."""
    def __init__(self) -> None:
        self._threshold = 0.7
        self._runtime_state = {'open': ['BTCUSD']}

    def report_values(self) -> Dict[str, float]:
        return {'threshold': self._threshold}


class _Opaque:
    def __init__(self) -> None:
        self._secret = 'must never reach a payload'


def test_a_dataclass_carries_its_fields_and_its_public_properties() -> None:
    """`dataclasses.asdict` walks fields only — that is the gap this module exists to close."""
    row = _Row(name='theblock', hits=99, total=100)

    result = to_jsonable(row)

    assert result['name'] == 'theblock'
    assert result['rate'] == pytest.approx(0.99)     # the property, which asdict would drop
    assert '_hidden' not in result                   # private ones stay private


def test_nested_dataclasses_lists_and_dicts_are_walked() -> None:
    report = _Report(rows=[_Row('a', 1, 2), _Row('b', 0, 0)], labels={'x': 1})

    result = to_jsonable(report)

    assert result['count'] == 2                      # the container's own property
    assert [row['rate'] for row in result['rows']] == [0.5, 0.0]
    assert result['labels'] == {'x': 1}


def test_datetimes_pass_through_untouched() -> None:
    """Rendering them is the transport's job; choosing a format here would be guessing."""
    moment = datetime(2026, 8, 25, 6, 30, tzinfo=timezone.utc)

    assert to_jsonable(_Row('a', 1, 1, when=moment))['when'] == moment


def test_a_duration_becomes_seconds() -> None:
    """JSON has no duration type, and a number beats a string the caller has to parse."""
    assert to_jsonable(timedelta(minutes=150)) == 9000.0


def test_a_set_serializes_sorted_so_two_identical_reports_match() -> None:
    assert to_jsonable({'c', 'a', 'b'}) == ['a', 'b', 'c']


def test_a_behaviour_object_publishes_its_values_not_its_state() -> None:
    """`report_values()` is the opt-in — and the reason it is not called `describe()`.

    `StoryGrouping.describe()` already exists and returns a console line. One name for "render me"
    and "serialize me" would put a display string where a number belongs, silently.
    """
    result = to_jsonable(_Policy())

    assert result == {'threshold': 0.7}
    assert 'open' not in str(result)                 # the running state stayed behind


def test_an_unknown_object_raises_instead_of_leaking_its_dunder_dict() -> None:
    """FastAPI's encoder would happily serialize this via `vars()` — publishing internals as data.

    Failing here means a report shape that would leak is caught by a test, not by a reader of the
    payload noticing something that should not be there.
    """
    with pytest.raises(TypeError, match='cannot appear in a report payload'):
        to_jsonable(_Opaque())


def test_the_error_names_the_type_and_the_way_out() -> None:
    with pytest.raises(TypeError) as excinfo:
        to_jsonable(_Opaque())

    message = str(excinfo.value)
    assert '_Opaque' in message and 'report_values()' in message
