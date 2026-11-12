"""Signal packages — the shapes that cross the packer, the store, the loop and the routes.

The manifest itself is a JSON document whose top-level keys are a contract shared with the
collector's tick packages (testingide#120): one client reads both services. It is built in one place
(`day_packer.build_manifest`) and served as written, so it stays a plain mapping here; what crosses
the seams around it — a run's outcome, a package found by id — is typed.
"""
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

# What one run did for one source. `refused` is a precondition that failed before anything was
# written (no mint, a full disk, a held lock, a cutoff not yet closed); `failed` is an error after.
PackRunResult = Literal['packed', 'nothing_new', 'refused', 'failed']
# A package is `ready` until a verified receipt arrives, then `delivered`. It stays listed either way.
PackageState = Literal['ready', 'delivered']

# `<source>-<sequence, 4+ digits>-<cutoff YYYYMMDD>`, read from the right: a pipeline id carries
# underscores, so the source is whatever precedes the last two parts.
PACKAGE_ID = re.compile(r'^(?P<source>[a-z0-9_]+)-(?P<seq>\d{4,})-(?P<cutoff>\d{8})$')
# A member is one pipeline's one UTC day: `<pipeline_id>_<YYYY-MM-DD>.jsonl`, read from the right —
# the last ten characters before the extension are the day (agreed with the Testing IDE, 2026-11-12).
MEMBER_SUFFIX = '.jsonl'

PRODUCER = 'finiex-rag-engine'
PACKER_PROGRAM = 'finiex-rag-packages'
MANIFEST_SCHEMA = 1


def member_name(source: str, day: str) -> str:
    """The member file name of one pipeline's one UTC day."""
    return f'{source}_{day}{MEMBER_SUFFIX}'


@dataclass
class SourceRun:
    """What one package run did for one source — written to `last_run.json`, served on health."""
    source: str
    result: str                         # PackRunResult
    at: str                             # UTC ISO, when the run finished
    cutoff: str                         # UTC ISO midnight
    duration_seconds: float
    reason: Optional[str] = None
    package_id: Optional[str] = None
    member_count: int = 0
    record_count: int = 0
    zip_size_bytes: int = 0
    excluded: List[Dict[str, str]] = field(default_factory=list)
    removed_incomplete: List[str] = field(default_factory=list)
    entered_late: List[str] = field(default_factory=list)

    def to_record(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FoundPackage:
    """One committed package as the store holds it: its state, manifest, zip and receipt."""
    package_id: str
    source: str
    state: str                          # PackageState
    manifest: Dict[str, Any]
    zip_path: Path
    receipt: Optional[Dict[str, Any]] = None


@dataclass
class LedgerRead:
    """Events read back from the ledger, oldest first, with the lines that could not be parsed."""
    count: int
    unreadable_lines: int
    events: List[Dict[str, Any]]
