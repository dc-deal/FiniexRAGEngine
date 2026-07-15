"""Schema-migration domain types (ISSUE_14) — what the runner found and what it did.

The shapes the `MigrationRunner` produces and the migrate CLI / boot check consume.
Behaviour lives in `core/schema/`; only the shapes live here.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class Migration:
    """One migration file on disk: `001_init.sql` -> version '001', name 'init'."""
    version: str
    name: str
    path: str
    checksum: str               # sha256 of the file body — drift detection, see MigrationStatus
    # False only for a file carrying `-- finiex:no-transaction`, for the handful of statements
    # PostgreSQL refuses inside a transaction block (CREATE INDEX CONCURRENTLY, ALTER TYPE ...
    # ADD VALUE). Such a file gives up all-or-nothing and must hold exactly one statement.
    transactional: bool = True


@dataclass
class AppliedMigration:
    """One recorded row from `schema_migrations` — what the database believes it has run."""
    version: str
    name: str
    applied_at: datetime
    checksum: str


@dataclass
class MigrationStatus:
    """The comparison of disk against database — the whole state in one shape.

    `drifted` is the loud case: a file whose checksum no longer matches what was applied.
    Re-running cannot fix it (the version is recorded), so the runner refuses rather than
    pretend the schema matches the repo.
    """
    applied: List[AppliedMigration] = field(default_factory=list)
    pending: List[Migration] = field(default_factory=list)
    drifted: List[Migration] = field(default_factory=list)

    @property
    def is_current(self) -> bool:
        return not self.pending and not self.drifted


@dataclass
class MigrationRun:
    """What one apply pass did — one entry per migration actually executed."""
    version: str
    name: str
    duration_ms: float
    error: Optional[str] = None
