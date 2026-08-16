"""Fingerprint registry (ISSUE_85) — keeps what each `config_fingerprint` stood for.

The scalar on the envelope says *that* a setup changed; this table says *what* it was. One row
per distinct configuration, written at assembly, read by a later investigation — the same role
and lifecycle as `source_poll_log`, which is why it lives here rather than with the config
managers (no DB access there by design) or next to the outcome store (that one is on the
serving path).

**A registry write never fails an assembly.** Every DB error is logged and swallowed: the
fingerprint is already stamped on the envelope without this table, which only explains it.
Losing a row costs an explanation, not a signal — the same trade `SourcePollLog` makes, and the
opposite of `SourceHealthStore`, which raises because health drives a reach decision.
"""
import logging
from datetime import datetime, timezone

import psycopg

from finiexragengine.types.config_fingerprint_types import ConfigFingerprint

logger = logging.getLogger(__name__)


class ConfigFingerprintStore:
    """Upserts resolved configurations into `config_fingerprints`."""

    def __init__(self, database_url: str, table: str = 'config_fingerprints') -> None:
        self._database_url = database_url
        self._TABLE = table

    def register(self, fingerprint: ConfigFingerprint) -> bool:
        """Record this setup; returns True when it was seen for the very first time.

        The boolean is what turns the boot line into a warning worth reading: `(new)` means this
        start breaks the comparable series, which is the moment nobody noticed on 2026-07-24.
        A swallowed DB error reports False — an unknown answer must not claim novelty.
        """
        now = datetime.now(timezone.utc)
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                # `xmax = 0` is the standard way to tell an INSERT from an ON CONFLICT UPDATE:
                # a freshly inserted row carries no deleting transaction id.
                cur.execute(
                    f'INSERT INTO {self._TABLE} (fingerprint, pipeline_id, source_set_id, '
                    'config, first_seen, last_seen) VALUES (%s, %s, %s, %s::jsonb, %s, %s) '
                    'ON CONFLICT (fingerprint) DO UPDATE SET last_seen = EXCLUDED.last_seen '
                    'RETURNING (xmax = 0)',
                    (fingerprint.value, fingerprint.pipeline_id, fingerprint.source_set_id,
                     fingerprint.canonical, now, now))
                return bool(cur.fetchone()[0])
        except psycopg.Error as exc:
            logger.warning('config fingerprint %s not registered (provenance only, boot '
                           'continues): %s', fingerprint.value, exc)
            return False
