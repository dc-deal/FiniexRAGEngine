"""Boot-time instance-identity check (ISSUE_9 follow-up) — refuse to produce unattributable rows.

Its own unit beside `schema_guard`, and for the same reason: every DB-touching path builds its
object graph through the assembler, so that is where a boot check belongs — neither the API nor a
CLI should have to reach into the store for one.

**Checks, never mints.** Minting is `017_journal_identity.sql`'s job alone; its header carries the
argument. An identity minted at boot would change at every restart, and the consumer merges series
on this field — a new value is a claim that a different producer wrote the rows.

What it protects against is narrow and real: the migration is what mints, so a current schema always
has a row, and the only ways to lose one are a hand DELETE or a table restored from before this
migration. Both produce the same silent damage — every envelope from then on carries `instance_id:
''`, which a consumer reads as "produced before the field existed". That is a plausible default
papering over a failure, so it fails the boot instead.
"""
import logging
import re

import psycopg

from finiexragengine.exceptions.ragengine_errors import ConfigurationError, VectorStoreError

logger = logging.getLogger(__name__)

# The wire format, the same one migration 017's CHECK enforces. Stated twice on purpose: the
# constraint protects the table from a bad write, this protects the consumer from a table that
# predates the constraint — a schema restored from an older dump satisfies the column type and not
# the format the parser downstream was built against.
_INSTANCE_ID = re.compile(r'^[0-9a-f]{12}$')

# Matches `OutcomeStore._connect`: a healthy local connect measures ~6 ms, and this one runs on the
# boot path where the schema guard has just connected successfully.
_CONNECT_TIMEOUT_SECONDS = 5


def verify_instance_identity(database_url: str) -> str:
    """Return this deployment's `instance_id`, or raise when the schema cannot name itself.

    Raises:
        ConfigurationError: the identity row is missing or malformed — the schema is migrated but
            cannot say which deployment owns it.
        VectorStoreError: the database is unreachable.
    """
    try:
        with psycopg.connect(database_url, connect_timeout=_CONNECT_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT instance_id, minted_at FROM journal_identity')
                row = cur.fetchone()
    except psycopg.Error as exc:
        raise VectorStoreError(f'cannot read the instance identity: {exc}') from exc

    if row is None:
        raise ConfigurationError(
            'the schema carries no instance identity — migration 017 minted one and the row is '
            'gone. Every envelope would be stamped with an empty producer, which a consumer reads '
            'as "produced before the field existed". Re-mint deliberately (see '
            'docs/development/diagnostics.md — "Which instance am I looking at?"); note that a '
            'new id is a discontinuity in the consumer\'s series, never a repair.')
    instance_id, minted_at = row[0], row[1]
    if not _INSTANCE_ID.fullmatch(instance_id or ''):
        raise ConfigurationError(
            f'the instance identity {instance_id!r} is not 12 lowercase hex — that format is the '
            'consumer contract, and an envelope carrying anything else reaches their loader as a '
            'parse error. Correct it in `journal_identity` before booting.')
    # The census, once per boot: the value a consumer will see on every envelope this process
    # writes, with the day the deployment began. Printed rather than merely checked — an identity
    # nobody ever reads in a log is one nobody notices changing.
    logger.info('[IDENTITY] instance %s · minted %s', instance_id, minted_at.isoformat())
    return instance_id
