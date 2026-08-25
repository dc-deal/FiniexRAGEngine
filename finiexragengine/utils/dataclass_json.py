"""Turn a report's dataclass tree into JSON-serializable data (ISSUE_104).

`dataclasses.asdict` is not enough, and the gap is not cosmetic: it walks **fields only**, so every
`@property` disappears. The reports put derived values there on purpose — `SourceHealthRow.
success_rate`, `SourceHealthReport.flagged_count`, and above all `quarantined`, which compares
`quarantined_until` against *now* and is therefore a verdict only the server can give. Serializing
fields alone would ship an API payload that says something different from the console rendering of
the same report.

So the rule here is one line long and applies to every report equally: **a dataclass becomes its
fields plus its public properties.** That is what makes this a serializer rather than thirteen
hand-written mirror models — the shape `types/api_types.WorkerInfo` uses, which is right for four
fields on `/health` and would be duplication at this scale.

A property that raises is left to raise. It would be a defect in a report, and answering with a
partial payload would hide it while making the API disagree with the console — the two failure modes
this module exists to prevent.

Datetimes are passed through untouched: FastAPI's encoder renders them as ISO-8601, and converting
here would mean deciding a format in a place that cannot know the transport.

**Anything else raises.** A report's fields are not all data: `BreakingReport.rules_applied` carries
the `BreakingEpisodeRule` objects the console renderer prints its policy line from. FastAPI's encoder
would happily serialize such an object by falling back to `vars()` — publishing an engine unit's
private state (`_open`, `_gap`) as if it were a measurement. So an object that belongs in a payload
says so, by implementing `report_values()` and returning the *values* that matter; everything else
is a `TypeError` here, where a test sees it, rather than a surprise on the wire.

The name is `report_values`, not `describe`, deliberately: `StoryGrouping.describe()` already exists
and returns a **console line** (`story >=0.45 - within 72h`). One name for "render me" and "serialize
me" would put a display string where a number belongs, and nothing would complain.
"""
import dataclasses
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Tuple, Type

# What may pass through untouched. Deliberately a list rather than "everything that is not a
# container": the point is that an unrecognised object is an error, not a guess.
_PASSTHROUGH = (str, int, float, bool, datetime, date, time, Decimal, type(None))


def _public_properties(cls: Type[Any]) -> Tuple[str, ...]:
    """Property names on `cls` and its bases, excluding the private ones.

    Walked over the MRO rather than `vars(cls)` so a shared base's derived values are carried too.
    """
    names: List[str] = []
    for klass in reversed(cls.__mro__):
        for name, member in vars(klass).items():
            if isinstance(member, property) and not name.startswith('_') and name not in names:
                names.append(name)
    return tuple(names)


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, containers and scalars into JSON-serializable data."""
    # `is_dataclass` is true for the CLASS as well as an instance; only an instance has values.
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result: Dict[str, Any] = {field.name: to_jsonable(getattr(value, field.name))
                                  for field in dataclasses.fields(value)}
        for name in _public_properties(type(value)):
            result[name] = to_jsonable(getattr(value, name))
        return result
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        # Sorted where the members allow it: an unordered set would otherwise make two identical
        # reports serialize differently, which turns a diff between two runs into noise.
        try:
            return [to_jsonable(item) for item in sorted(value)]
        except TypeError:
            return [to_jsonable(item) for item in value]
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, _PASSTHROUGH):
        return value
    if isinstance(value, timedelta):
        # Rendered as seconds: a duration has no JSON type, and a number the caller can compare
        # beats a string it has to parse.
        return value.total_seconds()
    report_values = getattr(value, 'report_values', None)
    if callable(report_values):
        # The opt-in for behaviour objects: they publish the values that explain a report (a rule's
        # gap and threshold), never their internal state.
        return to_jsonable(report_values())
    raise TypeError(
        f'{type(value).__name__} cannot appear in a report payload: it is neither data nor a '
        f'unit that describes itself. Give it a report_values() returning the values that matter, '
        f'or keep it out of the report shape.')
