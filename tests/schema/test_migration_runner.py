"""Tests for the schema-migration runner (ISSUE_14).

Needs a reachable Postgres (skipped otherwise); no API budget. Each test builds its own
migrations directory in a tmp_path and its own throwaway schema, so nothing here depends on the
repo's real `migrations/` — those are exercised by the `db_dsn` fixture, which builds every
DB test's schema from them.
"""
from pathlib import Path
from typing import Iterator

import psycopg
import pytest

from finiexragengine.core.schema.migration_runner import MigrationRunner
from finiexragengine.core.schema.schema_guard import verify_schema_current
from finiexragengine.exceptions.ragengine_errors import ConfigurationError, VectorStoreError

_SCHEMA = 'finiex_migration_test'


@pytest.fixture
def dsn(db_dsn: str) -> Iterator[str]:
    """A second throwaway schema — this suite writes its own DDL, so it stays off `db_dsn`'s."""
    base = db_dsn.split('?', 1)[0]
    with psycopg.connect(base) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE')
        conn.execute(f'CREATE SCHEMA {_SCHEMA}')
        conn.commit()
    yield f'{base}?options=-csearch_path%3D{_SCHEMA},public'
    with psycopg.connect(base) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE')
        conn.commit()


def _write(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(sql)


def test_applies_in_order_and_records(dsn, tmp_path):
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY);')
    _write(migrations, '002_add_label.sql', 'ALTER TABLE widget ADD COLUMN label TEXT;')
    # 002 only applies if 001 ran first — ordering is the assertion.
    runs = MigrationRunner(dsn, migrations).apply()
    assert [r.version for r in runs] == ['001', '002']
    status = MigrationRunner(dsn, migrations).status()
    assert [a.version for a in status.applied] == ['001', '002']
    assert status.is_current


def test_rerun_is_a_noop(dsn, tmp_path):
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY);')
    MigrationRunner(dsn, migrations).apply()
    # A second pass must not re-execute (the CREATE would raise "already exists").
    assert MigrationRunner(dsn, migrations).apply() == []


def test_new_migration_adds_a_column_to_a_populated_table(dsn, tmp_path):
    # The whole point of ISSUE_14: a schema change reaches a table that already holds data.
    # (Replaces the old in-place `ALTER ... IF NOT EXISTS` in CostRecorder, now retired.)
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY);')
    MigrationRunner(dsn, migrations).apply()
    with psycopg.connect(dsn) as conn:
        conn.execute('INSERT INTO widget (id) VALUES (1), (2)')
        conn.commit()

    _write(migrations, '002_add_label.sql', 'ALTER TABLE widget ADD COLUMN label TEXT;')
    MigrationRunner(dsn, migrations).apply()

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*), count(label) FROM widget')
        assert cur.fetchone() == (2, 0)          # rows survived; the new column is NULL


def test_failed_migration_rolls_back_completely(dsn, tmp_path):
    # PostgreSQL has transactional DDL: the file and its schema_migrations row commit together,
    # so a broken migration must leave *nothing* behind — not even its first statement.
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_broken.sql',
           'CREATE TABLE widget (id INT PRIMARY KEY); SELECT nonexistent_function();')
    with pytest.raises(VectorStoreError, match='001_broken'):
        MigrationRunner(dsn, migrations).apply()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('widget')")
        assert cur.fetchone()[0] is None                    # the CREATE rolled back too
    assert MigrationRunner(dsn, migrations).status().applied == []   # nothing recorded


def test_edited_applied_migration_is_drift_and_refuses(dsn, tmp_path):
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY);')
    MigrationRunner(dsn, migrations).apply()
    # Editing an applied file cannot be fixed by re-running (the version is recorded), so the
    # runner must refuse rather than pretend the live schema matches the repo.
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY, extra TEXT);')
    status = MigrationRunner(dsn, migrations).status()
    assert [m.version for m in status.drifted] == ['001']
    assert not status.is_current
    with pytest.raises(ConfigurationError, match='changed after being applied'):
        MigrationRunner(dsn, migrations).apply()


def test_duplicate_version_refuses(dsn, tmp_path):
    # Two files claiming one version would apply in an arbitrary order — never silently.
    migrations = tmp_path / 'migrations'
    _write(migrations, '002_alpha.sql', 'SELECT 1;')
    _write(migrations, '002_beta.sql', 'SELECT 1;')
    with pytest.raises(ConfigurationError, match='duplicate migration version'):
        MigrationRunner(dsn, migrations).discover()


def test_non_migration_files_are_ignored(dsn, tmp_path):
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY);')
    _write(migrations, 'README.md', 'not a migration')
    _write(migrations, 'draft.sql', 'DROP TABLE widget;')   # unnumbered -> never runs
    assert [m.version for m in MigrationRunner(dsn, migrations).discover()] == ['001']


def test_missing_directory_fails_loudly(dsn, tmp_path):
    with pytest.raises(ConfigurationError, match='migrations directory not found'):
        MigrationRunner(dsn, tmp_path / 'nope').discover()


# --- the no-transaction escape hatch --------------------------------------------------

def test_concurrent_index_needs_the_marker(dsn, tmp_path):
    # The gap the marker closes, pinned so it cannot return silently: PostgreSQL refuses
    # CREATE INDEX CONCURRENTLY inside a transaction block, and every ordinary file gets one.
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY, label TEXT);')
    _write(migrations, '002_idx.sql',
           'CREATE INDEX CONCURRENTLY idx_widget_label ON widget (label);')
    with pytest.raises(VectorStoreError, match='cannot run inside a transaction block'):
        MigrationRunner(dsn, migrations).apply()


def test_no_transaction_marker_runs_a_concurrent_index(dsn, tmp_path):
    # The fix: same DDL, one comment line -> autocommit connection -> it builds.
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY, label TEXT);')
    _write(migrations, '002_idx.sql',
           '-- finiex:no-transaction\n'
           'CREATE INDEX CONCURRENTLY idx_widget_label ON widget (label);')
    assert [r.version for r in MigrationRunner(dsn, migrations).apply()] == ['001', '002']

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT indisvalid FROM pg_index "
                    "WHERE indexrelid = 'idx_widget_label'::regclass")
        assert cur.fetchone() == (True,)             # built, and a valid index
    # Ledger-recorded like any other migration, so a re-run stays a no-op.
    assert MigrationRunner(dsn, migrations).apply() == []


def test_no_transaction_with_two_statements_refuses(dsn, tmp_path):
    # autocommit alone is NOT enough: several statements in one query get an *implicit*
    # transaction and hit the very error the marker exists to avoid. Rejected at discovery,
    # so --status surfaces it before anything touches the schema.
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_two.sql',
           '-- finiex:no-transaction\n'
           'CREATE INDEX CONCURRENTLY idx_a ON widget (label);\n'
           'CREATE INDEX CONCURRENTLY idx_b ON widget (id);')
    with pytest.raises(ConfigurationError, match='more than one statement'):
        MigrationRunner(dsn, migrations).discover()


def test_marker_must_be_its_own_comment_line(dsn, tmp_path):
    # Prose mentioning the marker must not switch a file off transactions by accident —
    # otherwise a migration silently loses its rollback because of a doc comment.
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql',
           '-- unlike finiex:no-transaction files, this one is atomic\n'
           'CREATE TABLE widget (id INT PRIMARY KEY);\n'
           'CREATE TABLE gadget (id INT PRIMARY KEY);')
    assert MigrationRunner(dsn, migrations).discover()[0].transactional is True


# --- the boot guard -------------------------------------------------------------------

def test_boot_check_refuses_when_behind(dsn, tmp_path):
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY);')
    with pytest.raises(ConfigurationError, match='1 migration behind'):
        verify_schema_current(dsn, migrations)


def test_boot_check_passes_when_current(dsn, tmp_path):
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY);')
    MigrationRunner(dsn, migrations).apply()
    verify_schema_current(dsn, migrations)               # no raise = current


def test_boot_check_never_applies(dsn, tmp_path):
    # The guard checks; only the migrate CLI writes. A deploy must not mutate a database as a
    # side effect of booting (and three cloud containers must not race to do it).
    migrations = tmp_path / 'migrations'
    _write(migrations, '001_init.sql', 'CREATE TABLE widget (id INT PRIMARY KEY);')
    with pytest.raises(ConfigurationError):
        verify_schema_current(dsn, migrations)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('widget')")
        assert cur.fetchone()[0] is None                  # the guard created nothing
