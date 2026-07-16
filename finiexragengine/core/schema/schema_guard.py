"""Boot-time schema check (ISSUE_14) — refuse to run against a schema behind the repo.

Its own unit because both the API and every DB-touching CLI import it, and neither should have
to reach into the runner for it.

**Checks, never applies.** Applying is the migrate CLI's job alone, for two reasons: a deploy
must never silently mutate a database, and in the split-worker model (ingest / eval / API as
separate processes — separate containers in the cloud) three boots would otherwise each try to
migrate at once. The runner's advisory lock makes that *safe*, but it should not happen at all:
a schema change is an operator action, not a side effect of starting a process.
"""
import logging
from pathlib import Path

from finiexragengine.core.schema.migration_runner import MigrationRunner
from finiexragengine.exceptions.ragengine_errors import ConfigurationError

logger = logging.getLogger(__name__)


def verify_schema_current(database_url: str, migrations_dir: Path) -> None:
    """Raise unless every migration on disk is applied. No-op when the schema is current.

    Fails loudly and early — before any worker starts or any paid call happens — because a
    stale schema surfaces otherwise as a confusing `column does not exist` mid-pass.
    """
    status = MigrationRunner(database_url, migrations_dir).status()
    if status.drifted:
        names = ', '.join(f'{m.version}_{m.name}' for m in status.drifted)
        raise ConfigurationError(
            f'migration file(s) changed after being applied: {names} — the live schema no longer '
            'matches the repo. Revert the edit, or express the change as a NEW migration.')
    if status.pending:
        names = ', '.join(f'{m.version}_{m.name}' for m in status.pending)
        count = len(status.pending)
        raise ConfigurationError(
            f'schema is {count} migration{"s" if count > 1 else ""} behind '
            f'(pending: {names}) — run `python -m finiexragengine.cli.migrate_cli` '
            'before starting the engine')
    logger.debug('[MIGRATE] schema current (%d applied)', len(status.applied))
