"""Read the engine's own log file over a UTC time range (2026-09-08).

Every other diagnostic answers over HTTP since ISSUE_104; the log did not, so the one incident where
the reports were not enough — four DNS outages on 2026-09-08, whose cause was a single word
(`getaddrinfo failed`) buried in a traceback — needed an RDP session and a copied file.

**The timezone is the whole difficulty, and it is not a detail.** The engine is UTC throughout, as
CLAUDE.md requires. The *logging formatter* is not: it stamps the OS clock, and the server runs
GMT+2. One production line carries both at once —

    2026-09-08T04:40:43.978+02:00 … [HOST] host connectivity — … retry 02:45:43 UTC
    └─ the formatter: local time                                 └─ the app: UTC

— the same instant, twice, in two clocks. Because the offset is written out, the conversion is
lossless; because it is *not* UTC, a naive string comparison against a UTC `since` is silently two
hours wrong. So every line's prefix is parsed offset-aware, compared in UTC, and returned in UTC
with a `Z`, which is what the rest of the API speaks (`utils/dataclass_json`).

Three more properties this unit owns, each because the obvious version is wrong:

- **A rotated file is part of a range.** Rotation is daily (`finiex.log.YYYY-MM-DD`, 14 kept), so a
  window reaching past midnight has to read the siblings too — otherwise "query a time range"
  quietly means "today".
- **A traceback belongs to its entry.** Continuation lines carry no timestamp of their own; they are
  attached to the preceding entry, so a filtered window never returns a stack fragment with no head
  — which is exactly the shape that would have been useless on 2026-09-08.
- **Redaction is counted, not silent.** A log line can carry a DSN password or a bearer token in an
  unhandled traceback. Those are replaced, and the answer says how many lines it changed: a reader
  trusts a line, so an altered one that does not say so is worse than a withheld one. The patterns
  themselves live in `finiex_auth.redaction` — one vocabulary for every surface that publishes
  text, shared with the Testing IDE, because the copy that is not updated is the one that leaks.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from finiex_auth.redaction import redact


# `2026-09-08T04:40:43.978+02:00 ERROR logger.name: message`
_ENTRY = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?'
    r'(?:Z|[+-]\d{2}:?\d{2})?)\s+(?P<level>[A-Z]+)\s+(?P<rest>.*)$')

# Ordered, so `min_level` is a floor rather than an exact match.
_LEVELS: Tuple[str, ...] = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
_LEVEL_RANK: Dict[str, int] = {name: index for index, name in enumerate(_LEVELS)}

@dataclass
class LogEntry:
    """One log entry — its head line plus any continuation (traceback) lines beneath it."""
    timestamp: datetime                       # always UTC, whatever the file was written in
    level: str
    message: str
    continuation: List[str] = field(default_factory=list)

    @property
    def lines(self) -> int:
        return 1 + len(self.continuation)


@dataclass
class LogPage:
    """What one query returned, and what it had to leave out."""
    since: Optional[datetime]
    until: Optional[datetime]
    min_level: str
    entries: List[LogEntry] = field(default_factory=list)
    files_read: List[str] = field(default_factory=list)
    matched: int = 0                          # entries in range before the limit was applied
    redacted_lines: int = 0

    @property
    def truncated(self) -> bool:
        """True when the range held more than `limit` — the caller is seeing the newest slice."""
        return self.matched > len(self.entries)


def parse_timestamp(raw: str) -> Optional[datetime]:
    """A log prefix to an aware UTC datetime — `None` when it is not a timestamp at all.

    A file written without an offset would be ambiguous, so a naive value is read as UTC rather
    than as local: guessing the writer's zone is how an off-by-two-hours becomes invisible.
    """
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def files_for_range(log_path: Path, since: Optional[datetime],
                    until: Optional[datetime]) -> List[Path]:
    """The live file plus every rotated sibling whose day can hold part of the range.

    The handler rolls over at **UTC** midnight (`configure_logging`, `utc=True`), so the suffix is
    the UTC day the file covers — measured on production: `finiex.log.2026-09-07` runs from local
    02:00 on the 7th to 01:53 on the 8th, which is exactly UTC 2026-09-07. The lines *inside* are
    stamped local, which is the mismatch worth knowing about.

    The day window is still widened by one on each side rather than matched exactly: the two clocks
    only line up while the handler stays on `utc=True`, and a cheap over-read cannot drop an entry
    where an exact match would silently lose the hours either side of midnight.
    """
    if not log_path.exists() and not log_path.parent.exists():
        return []
    candidates: List[Tuple[Optional[date], Path]] = []
    if log_path.exists():
        candidates.append((None, log_path))          # the live file: any date may still be in it
    for sibling in sorted(log_path.parent.glob(f'{log_path.name}.*')):
        stamp = sibling.name.rsplit('.', 1)[-1]
        try:
            day = date.fromisoformat(stamp)
        except ValueError:
            continue                                 # `.1`, `.gz`, anything not a daily rotation
        if since is not None and day < (since - timedelta(days=1)).date():
            continue
        if until is not None and day > (until + timedelta(days=1)).date():
            continue
        candidates.append((day, sibling))
    # Oldest first, live file last: entries then arrive in chronological order.
    return [path for _day, path in sorted(candidates, key=lambda item: (item[0] or date.max))]


def read_log(log_path: Path, *, since: Optional[datetime] = None,
             until: Optional[datetime] = None, min_level: str = 'WARNING',
             limit: int = 200) -> LogPage:
    """Entries in `[since, until]` at or above `min_level`, newest first, bounded by `limit`."""
    floor = _LEVEL_RANK.get(min_level.upper(), _LEVEL_RANK['WARNING'])
    page = LogPage(since=since, until=until, min_level=min_level.upper())
    matched: List[LogEntry] = []
    for path in files_for_range(log_path, since, until):
        page.files_read.append(path.name)
        current: Optional[LogEntry] = None
        keeping = False
        # `errors='replace'`: a truncated multi-byte character at a rotation boundary must not cost
        # the whole file — the point of this route is reading a log when things are already wrong.
        with path.open(encoding='utf-8', errors='replace') as handle:
            for raw in handle:
                line = raw.rstrip('\n')
                match = _ENTRY.match(line)
                if match is None:
                    # A traceback line. It belongs to the entry above it, or to nothing.
                    if keeping and current is not None:
                        masked, changed = redact(line)
                        current.continuation.append(masked)
                        page.redacted_lines += int(changed)
                    continue
                stamp = parse_timestamp(match.group('ts'))
                if stamp is None:
                    continue
                level = match.group('level')
                keeping = (_LEVEL_RANK.get(level, 0) >= floor
                           and (since is None or stamp >= since)
                           and (until is None or stamp <= until))
                if not keeping:
                    current = None
                    continue
                masked, changed = redact(match.group('rest'))
                page.redacted_lines += int(changed)
                current = LogEntry(timestamp=stamp, level=level, message=masked)
                matched.append(current)
    page.matched = len(matched)
    # Newest first, and the *newest* end is what a limit keeps: during an incident the question is
    # always "what just happened", never "what happened first in this window".
    page.entries = list(reversed(matched[-limit:])) if limit else []
    return page
