"""Console rendering of the migration status (ISSUE_14) — the migrate CLI's output surface.

Renders the shared console pattern (title + window line + `----` divider + aligned columns), so
the schema state reads like every other surface in the engine.
"""
from typing import List

from finiexragengine.types.schema_types import MigrationRun, MigrationStatus


def _dsn_label(database_url: str) -> str:
    """host:port/db from a DSN — never the credentials (this line goes into logs)."""
    tail = database_url.rsplit('@', 1)[-1]          # drop user:password@ when present
    return tail.split('?', 1)[0] or 'database'


def format_migration_status(status: MigrationStatus, database_url: str) -> str:
    """The `--status` view: what ran, what is waiting, what drifted."""
    lines = [f'=== Migrations: {_dsn_label(database_url)} ===']
    for applied in status.applied:
        lines.append(f'  applied   {applied.version}_{applied.name:28} '
                     f'{applied.applied_at:%Y-%m-%d %H:%M:%S}')
    for pending in status.pending:
        lines.append(f'  PENDING   {pending.version}_{pending.name}')
    for drifted in status.drifted:
        # Loud on purpose: the live schema no longer matches the repo, and re-running cannot fix it.
        lines.append(f'  DRIFTED   {drifted.version}_{drifted.name}   '
                     '(file changed after it was applied)')
    if not (status.applied or status.pending or status.drifted):
        lines.append('  (no migrations found)')
    lines.append('-' * 64)
    lines.append(f'---- {len(status.applied)} applied · {len(status.pending)} pending'
                 + (f' · {len(status.drifted)} DRIFTED' if status.drifted else ''))
    return '\n'.join(lines)


def format_migration_runs(runs: List[MigrationRun], database_url: str) -> str:
    """The apply view: one line per migration actually executed."""
    lines = [f'=== Migrations: {_dsn_label(database_url)} ===']
    if not runs:
        lines.append('  nothing pending — schema is current')
    for run in runs:
        lines.append(f'  applying {run.version}_{run.name} … ok ({run.duration_ms:.0f}ms)')
    lines.append('-' * 64)
    lines.append(f'---- {len(runs)} applied · 0 pending')
    return '\n'.join(lines)
