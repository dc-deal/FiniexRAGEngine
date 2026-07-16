"""CLI entry point: apply pending schema migrations (ISSUE_14) — the only writer of the schema.

`--status` is read-only. Without it, pending migrations are applied in order, each in its own
transaction. Re-running is a no-op, so this is safe to call before every deploy.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.migration_report import (
    format_migration_runs,
    format_migration_status,
)
from finiexragengine.core.schema.migration_runner import MigrationRunner


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Apply pending schema migrations (or show the status with --status)')
    parser.add_argument('--status', action='store_true',
                        help='show applied/pending migrations without changing anything')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    runner = MigrationRunner(database_url, AppConfigManager().get_migrations_dir())
    if args.status:
        print(format_migration_status(runner.status(), database_url))
        return
    print(format_migration_runs(runner.apply(), database_url))


if __name__ == '__main__':
    main()
