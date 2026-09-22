"""The dataclass tree ↔ JSON pair: out for a report payload (ISSUE_104), back for a viewer
(ISSUE_126).

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

**Datetimes are normalised to UTC and rendered with a `Z`.** Not cosmetics, twice over. The reports
read `TIMESTAMPTZ` columns, and psycopg hands those back in the *session's* timezone — on a server
running Europe/Berlin that is `+02:00`, so a report payload carried local time while every envelope
carried UTC. Same instant, two renderings, one of them silently dependent on the host's clock
settings, and CLAUDE.md says every datetime in this codebase is timezone-aware UTC.

The `Z` matters for a second reason: a `+` in a query string decodes as a space, so an offset-form
timestamp taken from one report could not be fed back into another (`?episode_start=...+02:00`
arrives as `... 02:00` and fails to parse). With `Z` there is no `+` to lose, and the value a report
prints is a value a caller can use.

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
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

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
    if isinstance(value, datetime):
        # `date`/`time` fall through to the passthrough below — only a full instant has a zone to
        # normalise. A naive datetime is left alone: it is a defect at its source, and quietly
        # stamping it UTC here would hide that.
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
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

def from_jsonable(cls: Type[Any], value: Any) -> Any:
    """Rebuild a dataclass tree from the data `to_jsonable` produced — the inbound half (ISSUE_126).

    Written for the viewer: the engine serializes its live state, a process on another machine walks
    it back, and the renderer is handed the same shapes it reads in-process. **Neither direction
    enumerates fields.** That is the property worth paying for — a measurement added to a snapshot
    reaches a remote screen with no edit at either end, which is the only version of this that
    survives six months (the FiniexDataCollector's words, and their reason: a viewer one commit newer
    than its producer drew correctly with nobody having planned for it).

    Reconstruction is driven by the ANNOTATIONS rather than by the data, so the two skew directions
    are decided rather than discovered:

    - a field the payload does not carry keeps the dataclass's own default, and a *required* one
      missing raises `TypeError` **naming it** — an older producer is a legible failure, not a
      half-built object;
    - a key the dataclass does not declare is dropped, so a newer producer draws on an older viewer.

    Datetimes come back from the `Z` form `to_jsonable` writes. A `Tuple[...]` annotation gets a
    tuple back, because JSON has no tuple and the renderer unpacks by position.
    """
    if not (dataclasses.is_dataclass(cls) and isinstance(cls, type)):
        raise TypeError(f'{cls!r} is not a dataclass, so there is nothing to rebuild into')
    if not isinstance(value, dict):
        raise TypeError(f'{cls.__name__} needs an object to rebuild from, got {type(value).__name__}')

    hints = get_type_hints(cls)
    kwargs: Dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        if field.name not in value:
            continue                        # absent -> the dataclass's own default, or its error
        kwargs[field.name] = _rebuild(hints.get(field.name, Any), value[field.name])
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise TypeError(f'{cls.__name__} cannot be rebuilt from this payload: {exc}') from None


def _rebuild(annotation: Any, value: Any) -> Any:
    """One value, against the annotation that says what it should become."""
    if value is None:
        return None
    origin = get_origin(annotation)
    if origin is Union:                     # Optional[X] and friends
        for candidate in get_args(annotation):
            if candidate is not type(None):
                return _rebuild(candidate, value)
        return value
    if origin in (list, List):
        (item,) = get_args(annotation) or (Any,)
        return [_rebuild(item, entry) for entry in value]
    if origin in (tuple, Tuple):
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_rebuild(args[0], entry) for entry in value)
        return tuple(_rebuild(arg, entry) for arg, entry in zip(args, value))
    if origin in (dict, Dict):
        args = get_args(annotation) or (Any, Any)
        return {key: _rebuild(args[1], item) for key, item in value.items()}
    if annotation is datetime or annotation is Optional[datetime]:
        return _parse_instant(value)
    if dataclasses.is_dataclass(annotation) and isinstance(annotation, type):
        return from_jsonable(annotation, value)
    return value


def _parse_instant(value: Any) -> Any:
    """`2026-09-22T13:25:41Z` back to an aware datetime; anything else is left alone.

    Left alone rather than raising: a value that is already a datetime passes through, and a string
    this function cannot read is a defect worth seeing at its source rather than here.
    """
    if isinstance(value, datetime) or not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return value
