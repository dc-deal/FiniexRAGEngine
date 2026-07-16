"""Versioned schema migrations (ISSUE_14) — apply pending SQL files, exactly once, in order.

Why hand-rolled: the established runners (Alembic, yoyo) earn their keep through ORM
autogenerate and rollback machinery, neither of which applies here — the engine is raw `psycopg`
with no ORM, and migrations are forward-only (a "down" that drops a column destroys data rather
than restoring it; that is a new migration, not a rollback). What they *do* have that a naive
runner lacks is a lock, and that part is borrowed below.

The mechanics:

- **Discovery**: `migrations/NNN_name.sql`, applied in filename order.
- **The lock**: a session-level `pg_advisory_lock` around the whole pass, so two processes
  booting at once (three separate containers, in the cloud model) cannot apply the same file
  twice. The loser waits, then finds nothing pending.
- **One transaction per file**: PostgreSQL has transactional DDL, so a failing migration leaves
  *nothing* half-applied — the file and its `schema_migrations` row commit together or not at all.
- **The escape hatch**: a few statements are exactly the ones PostgreSQL refuses inside a
  transaction block — `CREATE INDEX CONCURRENTLY` above all, which is how an index is built on a
  live corpus without locking writes. `-- finiex:no-transaction` runs that file on an autocommit
  connection instead. `autocommit` alone is not enough: several statements in one query are
  wrapped in an *implicit* transaction and hit the same error, so such a file is held to exactly
  one statement. That is also the right unit — without a transaction, the `schema_migrations`
  ledger is the only atomicity left, and it counts in whole files.
- **Checksums**: an already-applied file that changed on disk is drift. Re-running cannot fix it
  (the version is recorded), so the runner refuses loudly instead of pretending the live schema
  matches the repo.
"""
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Dict, List

import psycopg

from finiexragengine.exceptions.ragengine_errors import ConfigurationError, VectorStoreError
from finiexragengine.types.schema_types import (
    AppliedMigration,
    Migration,
    MigrationRun,
    MigrationStatus,
)

logger = logging.getLogger(__name__)

# `001_init.sql` -> version '001', name 'init'. Anything else in migrations/ is ignored.
_FILENAME = re.compile(r'^(\d{3})_([a-z0-9_]+)\.sql$')

# One arbitrary but FIXED key — every process must pick the same one for the lock to mean
# anything. Scoped to this engine's schema work; unrelated advisory locks are unaffected.
_LOCK_KEY = 0x46524147   # 'FRAG'

# The opt-out marker, on its own line anywhere in the file. A comment, so the file stays valid
# SQL that psql could run by hand — the directive is inert to everything except this runner.
# `\r` is tolerated: on a CRLF checkout a marker that silently failed to match would run the file
# in a transaction and raise the very error it exists to prevent.
_NO_TRANSACTION = re.compile(r'^[ \t]*--[ \t]*finiex:no-transaction[ \t\r]*$', re.MULTILINE)

# Comment strippers for the statement count below (not a SQL parser — see _statement_count).
_LINE_COMMENT = re.compile(r'--[^\n]*')
_BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)


def _checksum(body: str) -> str:
    """sha256 over the file body; whitespace-sensitive on purpose — any edit is drift.

    The directive lives in the body, so adding or removing it is drift like any other edit.
    """
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _statement_count(body: str) -> int:
    """Rough statement count — comments stripped, then split on `;`.

    Deliberately not a SQL parser: it guards one narrow rule (a no-transaction file holds one
    statement) and only ever runs on those files. A semicolon inside a string literal would
    over-count, which fails loudly with an actionable message ("one statement per file") rather
    than silently mis-executing — the safe direction to be wrong in.
    """
    stripped = _BLOCK_COMMENT.sub(' ', _LINE_COMMENT.sub(' ', body))
    return len([s for s in stripped.split(';') if s.strip()])


class MigrationRunner:
    """Discovers migration files, compares them against `schema_migrations`, applies the pending.

    Constructing it touches nothing: `status()` is read-only and `apply()` is the only mutation,
    so the boot check can ask "am I current?" without any risk of a silent schema change.
    """

    def __init__(self, database_url: str, migrations_dir: Path,
                 table: str = 'schema_migrations') -> None:
        self._database_url = database_url
        self._dir = migrations_dir
        self._table = table

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect for migrations: {exc}') from exc

    def discover(self) -> List[Migration]:
        """Every `NNN_name.sql` in the migrations dir, in version order."""
        if not self._dir.is_dir():
            raise ConfigurationError(f'migrations directory not found: {self._dir}')
        found: List[Migration] = []
        for path in sorted(self._dir.iterdir()):
            match = _FILENAME.match(path.name)
            if match is None:
                continue
            body = path.read_text()
            transactional = _NO_TRANSACTION.search(body) is None
            # Checked here, not at apply time: `--status` must be able to reject a malformed
            # file before anything touches the schema. Two statements would be wrapped in an
            # implicit transaction and fail with the very error the directive exists to avoid —
            # a baffling message, so it is turned into an actionable one up front.
            if not transactional and _statement_count(body) > 1:
                raise ConfigurationError(
                    f'{path.name} is marked `-- finiex:no-transaction` but holds more than one '
                    'statement — PostgreSQL wraps a multi-statement query in an implicit '
                    'transaction, which is exactly what the marker must avoid. Split it: one '
                    'statement per no-transaction file (the ledger tracks them separately).')
            found.append(Migration(version=match.group(1), name=match.group(2),
                                   path=str(path), checksum=_checksum(body),
                                   transactional=transactional))
        versions = [m.version for m in found]
        duplicate = {v for v in versions if versions.count(v) > 1}
        if duplicate:
            # Two files claiming one version would apply in an arbitrary order — never silent.
            raise ConfigurationError(f'duplicate migration version(s): {sorted(duplicate)}')
        return found

    def _ensure_version_table(self, cur: psycopg.Cursor) -> None:
        # The one piece of DDL that cannot itself be a migration — the bootstrap.
        cur.execute(f'CREATE TABLE IF NOT EXISTS {self._table} ('
                    'version TEXT PRIMARY KEY, '
                    'name TEXT NOT NULL, '
                    'applied_at TIMESTAMPTZ NOT NULL, '
                    'checksum TEXT NOT NULL)')

    def _applied(self, cur: psycopg.Cursor) -> Dict[str, AppliedMigration]:
        cur.execute(f'SELECT version, name, applied_at, checksum FROM {self._table} '
                    'ORDER BY version')
        return {v: AppliedMigration(version=v, name=n, applied_at=ts, checksum=cs)
                for v, n, ts, cs in cur.fetchall()}

    def status(self) -> MigrationStatus:
        """Read-only: what is applied, what is pending, what drifted. Never mutates the schema."""
        on_disk = self.discover()
        try:
            with self._connect() as conn, conn.cursor() as cur:
                self._ensure_version_table(cur)
                applied = self._applied(cur)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot read {self._table}: {exc}') from exc

        pending = [m for m in on_disk if m.version not in applied]
        drifted = [m for m in on_disk
                   if m.version in applied and applied[m.version].checksum != m.checksum]
        return MigrationStatus(applied=list(applied.values()), pending=pending, drifted=drifted)

    def apply(self) -> List[MigrationRun]:
        """Apply every pending migration in order, each in its own transaction. Re-run = no-op."""
        status = self.status()
        if status.drifted:
            names = ', '.join(f'{m.version}_{m.name}' for m in status.drifted)
            raise ConfigurationError(
                f'migration file(s) changed after being applied: {names} — the live schema no '
                'longer matches the repo. Revert the edit, or express the change as a NEW '
                'migration (applied migrations are immutable).')
        if not status.pending:
            return []

        runs: List[MigrationRun] = []
        # Session-level lock held across the whole pass: a concurrent booter blocks here and
        # finds nothing pending once it gets in. Its own connection, so a migration's own
        # transaction rollback can never drop the lock mid-pass.
        with self._connect() as lock_conn:
            lock_conn.autocommit = True
            with lock_conn.cursor() as cur:
                cur.execute('SELECT pg_advisory_lock(%s)', (_LOCK_KEY,))
            try:
                # Re-read under the lock: another process may have applied these while we waited.
                for migration in self.status().pending:
                    runs.append(self._apply_one(migration))
            finally:
                with lock_conn.cursor() as cur:
                    cur.execute('SELECT pg_advisory_unlock(%s)', (_LOCK_KEY,))
        return runs

    def _record(self, cur: psycopg.Cursor, migration: Migration) -> None:
        """Write the ledger row — the fact that makes a migration apply-once-ever."""
        cur.execute(f'INSERT INTO {self._table} (version, name, applied_at, checksum) '
                    'VALUES (%s, %s, %s, %s)',
                    (migration.version, migration.name,
                     datetime.now(timezone.utc), migration.checksum))

    def _apply_one(self, migration: Migration) -> MigrationRun:
        label = f'{migration.version}_{migration.name}'
        body = Path(migration.path).read_text()
        started = perf_counter()
        if migration.transactional:
            self._apply_in_transaction(label, body, migration)
        else:
            self._apply_without_transaction(label, body, migration)
        duration_ms = (perf_counter() - started) * 1000.0
        logger.info('[MIGRATE] applied %s (%.0fms)%s', label, duration_ms,
                    '' if migration.transactional else ' [no-transaction]')
        return MigrationRun(version=migration.version, name=migration.name,
                            duration_ms=duration_ms)

    def _apply_in_transaction(self, label: str, body: str, migration: Migration) -> None:
        """The normal path — PostgreSQL has transactional DDL, so use it.

        The file's statements and its `schema_migrations` row commit together or not at all: a
        half-applied migration is impossible, and a failure leaves the database untouched.
        """
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(body)
                    self._record(cur, migration)
                conn.commit()
        except psycopg.Error as exc:
            raise VectorStoreError(f'migration {label} failed (rolled back): {exc}') from exc

    def _apply_without_transaction(self, label: str, body: str, migration: Migration) -> None:
        """The `-- finiex:no-transaction` path: one statement on an autocommit connection.

        Atomicity is not given up by choice — PostgreSQL refuses these statements inside a
        transaction block, so there is none to be had. The ledger row goes on the same connection
        immediately after, keeping the "changed but unrecorded" window as small as possible.
        """
        try:
            with self._connect() as conn:
                conn.autocommit = True          # no implicit BEGIN — the whole point here
                with conn.cursor() as cur:
                    cur.execute(body)
                with conn.cursor() as cur:
                    self._record(cur, migration)
        except psycopg.Error as exc:
            # Never claim a rollback that did not happen: this path has no transaction to undo.
            raise VectorStoreError(
                f'migration {label} failed and was NOT rolled back: {exc} — it is marked '
                '`-- finiex:no-transaction`, so PostgreSQL had no transaction to undo and a '
                'partial effect may survive. A failed CREATE INDEX CONCURRENTLY leaves an '
                'INVALID index behind: DROP it before re-running.') from exc
