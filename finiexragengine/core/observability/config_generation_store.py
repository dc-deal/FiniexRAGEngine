"""Activation log (ISSUE_116) — when a configuration was live on a stream, not merely that it exists.

The registry next door (`config_fingerprint_store`) de-duplicates: one row per distinct
configuration, upserted, `first_seen`/`last_seen`. This appends: one row per **activation**, never
updated. Two different questions, and the registry cannot answer the second — A → B → A moves only
A's `last_seen`, so the re-activation reads as B nested inside A, and the boot line's `(new)` marker
is silent for a fingerprint that has been seen before. A live rollback is therefore invisible exactly
when it matters (ISSUE_115's rollback promise rests on this table).

**A log write never fails an activation.** Every DB error is logged and swallowed, the same trade the
registry makes and the opposite of `SourceHealthStore`: losing a row costs an explanation, never a
signal — and a boot that died because its provenance write failed would be the worst possible trade.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg

from finiexragengine.types.config_fingerprint_types import ConfigFingerprint

logger = logging.getLogger(__name__)

_BOOT = 'boot'
_ROLLBACK = 'rollback'


class ConfigGenerationStore:
    """Appends one row per generation activation into `config_generations`."""

    def __init__(self, database_url: str, table: str = 'config_generations') -> None:
        self._database_url = database_url
        self._TABLE = table

    def log_activation(self, fingerprint: ConfigFingerprint, *, process_started_at: datetime,
                       reason: Optional[str] = None) -> str:
        """Record this activation; returns the reason recorded, so the caller can print it.

        `reason` omitted is the normal case and the reason is **derived** rather than assumed: the
        previous activation on this stream is read in the same connection, and a generation that
        follows a *different* one while having been active on this stream before is a `rollback`.
        Nothing else can tell that apart — which is the whole point of the table.

        A swallowed DB error reports `boot`: an unknown answer must not claim a rollback.
        """
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                recorded = reason or self._derive_reason(cur, fingerprint)
                cur.execute(
                    f'INSERT INTO {self._TABLE} (fingerprint, pipeline_id, activated_at, reason, '
                    'process_started_at) VALUES (%s, %s, %s, %s, %s)',
                    (fingerprint.value, fingerprint.pipeline_id, datetime.now(timezone.utc),
                     recorded, process_started_at))
                return recorded
        except psycopg.Error as exc:
            logger.warning('config generation %s not logged (provenance only, boot continues): %s',
                           fingerprint.value, exc)
            return _BOOT

    def _derive_reason(self, cur: psycopg.Cursor, fingerprint: ConfigFingerprint) -> str:
        """`rollback` when this generation was current before, lost the stream, and now has it back.

        Two facts decide it, and both come from this table rather than from the registry: what ran
        last on this stream, and whether this fingerprint ever ran on it. A restart of an unchanged
        configuration is a `boot` — the previous row carries the same fingerprint — and a genuinely
        new generation is a `boot` too, because there is nothing to roll back to.
        """
        cur.execute(f'SELECT fingerprint FROM {self._TABLE} WHERE pipeline_id = %s '
                    'ORDER BY id DESC LIMIT 1', (fingerprint.pipeline_id,))
        previous = cur.fetchone()
        if previous is None or previous[0] == fingerprint.value:
            return _BOOT
        cur.execute(f'SELECT 1 FROM {self._TABLE} WHERE pipeline_id = %s AND fingerprint = %s '
                    'LIMIT 1', (fingerprint.pipeline_id, fingerprint.value))
        return _ROLLBACK if cur.fetchone() else _BOOT
