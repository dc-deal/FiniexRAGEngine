"""Per-stream sequence and epoch for the output contract (ISSUE_9).

The consumer orders the signal series by `seq` and detects loss by finding a gap in it, so the one
promise this unit exists to keep is: **a gap means exactly one thing — a record that never
arrived.** Everything below follows from that.

`mint()` deliberately takes an **open cursor** instead of opening its own connection. Joining the
envelope's insert transaction is what makes the series gapless: `UPDATE ... RETURNING` holds a row
lock until COMMIT, which serialises the tail of every transaction on a stream, so a rolled-back
pass returns its number and mint order equals commit order. A separate connection would give a
`nextval`-shaped counter with all of `BIGSERIAL`'s problems and none of its convenience.

`reconcile()` runs once at boot and answers a different question: was this series *rewound*? A
restore resets the counter, the engine re-mints numbers the consumer already holds, and every new
frame then sits below their cursor and is silently ignored while the connection stays healthy —
the worst failure shape there is. Detection cannot rest on the database's own bookkeeping alone,
because a restore rewinds that too; hence the cluster fingerprint below.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.stream_types import EpochBump, StreamStamp

logger = logging.getLogger(__name__)


class StreamSequencer:
    """Mints `(seq, epoch, available_msc)` per stream and reconciles the series at boot."""

    def __init__(self, database_url: str, table: str = 'stream_seq',
                 outcomes_table: str = 'outcomes') -> None:
        self._database_url = database_url
        self._table = table
        self._outcomes_table = outcomes_table

    # --- the write path ---------------------------------------------------------------------

    def mint(self, cur: psycopg.Cursor, pipeline_id: str, now_msc: int) -> StreamStamp:
        """Advance this stream by one, inside the caller's transaction.

        `now_msc` is the sampled wall clock; the returned `available_msc` is that value **clamped**
        against the previous one. A backwards step is held rather than emitted, and counted — the
        consumer gates no-look-ahead on this stamp, so a clock correction that moved it backwards
        would make a snapshot visible slightly before it actually was.
        """
        cur.execute(f'INSERT INTO {self._table} (pipeline_id) VALUES (%s) '
                    'ON CONFLICT (pipeline_id) DO NOTHING', (pipeline_id,))
        # Every SET expression reads the row's pre-update values, so the clamp, the resync counter
        # and the max-correction all see the same `last_available_msc` — one statement, no read
        # -modify-write window, and the row lock is held from here to the caller's COMMIT.
        cur.execute(
            f'UPDATE {self._table} SET '
            '  seq = seq + 1, '
            '  available_msc_resyncs = available_msc_resyncs + CASE '
            '      WHEN last_available_msc IS NOT NULL AND %(now)s < last_available_msc '
            '      THEN 1 ELSE 0 END, '
            '  available_msc_max_correction_ms = GREATEST(available_msc_max_correction_ms, CASE '
            '      WHEN last_available_msc IS NOT NULL AND %(now)s < last_available_msc '
            '      THEN last_available_msc - %(now)s ELSE 0 END), '
            '  last_available_msc = GREATEST(%(now)s, COALESCE(last_available_msc, %(now)s)), '
            '  updated_at = now() '
            'WHERE pipeline_id = %(pid)s '
            'RETURNING seq, epoch, last_available_msc, available_msc_resyncs, '
            '          available_msc_max_correction_ms',
            {'now': now_msc, 'pid': pipeline_id})
        seq, epoch, available_msc, resyncs, max_correction = cur.fetchone()
        if available_msc != now_msc:
            logger.warning('[%s] clock stepped back %d ms — available_msc held at %d (resync #%d)',
                           pipeline_id, available_msc - now_msc, available_msc, resyncs)
        return StreamStamp(seq=seq, epoch=epoch, available_msc=available_msc,
                           resyncs=resyncs, max_correction_ms=max_correction)

    # --- the boot path ----------------------------------------------------------------------

    def reconcile(self, pipeline_ids: List[str]) -> List[EpochBump]:
        """Check every stream for a rewind; bump the epoch where one is found.

        Two kinds of evidence, both mechanical:

        * **the counter is behind its own journal** — it was reset while the outcomes survived;
        * **the cluster fingerprint changed** — PITR or a standby promotion starts a new timeline,
          and a restore into a fresh cluster carries a different system identifier.

        A logical dump/restore *in place* changes neither and is not detectable from inside the
        database at all; that shape stays a runbook step, with the stream's `cursor_ahead` control
        frame as its after-the-fact detector.

        A fresh row is not a rewind: a stream seen for the first time is seeded, never bumped.
        """
        bumps: List[EpochBump] = []
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cluster_id = self._cluster_id(cur)
                for pipeline_id in pipeline_ids:
                    bump = self._reconcile_one(cur, pipeline_id, cluster_id)
                    if bump is not None:
                        bumps.append(bump)
        except psycopg.Error as exc:
            raise VectorStoreError(f'stream reconciliation failed: {exc}') from exc
        for bump in bumps:
            # A series break is the loudest thing this engine can do to a consumer — never a debug line.
            logger.warning('[%s] series rewound (%s): seq %d -> %d, epoch %d -> %d',
                           bump.pipeline_id, bump.reason, bump.previous_seq, bump.new_seq,
                           bump.previous_epoch, bump.new_epoch)
        return bumps

    def _reconcile_one(self, cur: psycopg.Cursor, pipeline_id: str,
                       cluster_id: Optional[str]) -> Optional[EpochBump]:
        cur.execute(f'INSERT INTO {self._table} (pipeline_id, cluster_id) VALUES (%s, %s) '
                    'ON CONFLICT (pipeline_id) DO NOTHING', (pipeline_id, cluster_id))
        cur.execute(f'SELECT seq, epoch, cluster_id FROM {self._table} '
                    'WHERE pipeline_id = %s FOR UPDATE', (pipeline_id,))
        seq, epoch, stored_cluster = cur.fetchone()
        journal_seq, journal_epoch = self._journal_head(cur, pipeline_id)

        counter_behind = journal_seq > seq
        # An unreadable or not-yet-recorded fingerprint is not evidence of anything — only a
        # *changed* one is. This is what keeps a managed host (where pg_control_* is refused) from
        # bumping the epoch on every boot.
        cluster_changed = (cluster_id is not None and stored_cluster is not None
                           and cluster_id != stored_cluster)

        if not counter_behind and not cluster_changed:
            if cluster_id is not None and stored_cluster is None:
                cur.execute(f'UPDATE {self._table} SET cluster_id = %s, updated_at = now() '
                            'WHERE pipeline_id = %s', (cluster_id, pipeline_id))
            return None

        # Monotone, not an increment: a restore to a point *before* an earlier bump rewinds the
        # stored number, and a plain +1 would reissue it — two different series both carrying the
        # same epoch, which collides the consumer's (pipeline_id, stream_epoch, seq) archive key
        # and merges them silently. Three floors, strongest first:
        #   * the journal's own highest epoch — exact whenever the outcomes outlived the counter,
        #     which is the same evidence the seq comparison above uses;
        #   * the stored epoch, for the ordinary case where nothing was rewound;
        #   * the wall clock, which is what remains when journal AND counter were rolled back
        #     together. Second resolution, so it is a strong anchor rather than a proof: two
        #     restores inside one second past a previous bump would still collide. That shape is
        #     not defended here — the runbook and the consumer's `cursor_ahead` report are.
        new_epoch = max(epoch + 1, journal_epoch + 1,
                        int(datetime.now(timezone.utc).timestamp()))
        new_seq = max(seq, journal_seq)
        cur.execute(f'UPDATE {self._table} SET seq = %s, epoch = %s, cluster_id = %s, '
                    '  last_available_msc = NULL, updated_at = now() '
                    'WHERE pipeline_id = %s', (new_seq, new_epoch, cluster_id, pipeline_id))
        return EpochBump(
            pipeline_id=pipeline_id, previous_epoch=epoch, new_epoch=new_epoch,
            reason='counter_behind_journal' if counter_behind else 'cluster_changed',
            previous_seq=seq, new_seq=new_seq, cluster_id=cluster_id)

    def _journal_head(self, cur: psycopg.Cursor, pipeline_id: str) -> Tuple[int, int]:
        """The highest `seq` and `stream_epoch` this stream ever persisted; (0, 0) when it never has.

        The journal is the stronger witness of the two: whenever the outcomes outlived a reset
        counter, it remembers both how far the series ran *and* which epoch it ran under, so the
        next epoch can be chosen to exceed one that was actually used rather than merely one that
        happens to be stored. Envelopes written before ISSUE_9 carry neither field; they are
        legitimately absent, not zero, and `max()` over JSONB nulls skips them.
        """
        cur.execute(f"SELECT coalesce(max((envelope->>'seq')::BIGINT), 0), "
                    f"       coalesce(max((envelope->>'stream_epoch')::BIGINT), 0) "
                    f'FROM {self._outcomes_table} WHERE pipeline_id = %s', (pipeline_id,))
        seq, epoch = cur.fetchone()
        return int(seq), int(epoch)

    def _cluster_id(self, cur: psycopg.Cursor) -> Optional[str]:
        """`<system_identifier>/<timeline_id>` — the part a restore cannot rewind.

        Returns None when the functions are not readable. On a managed Postgres the engine's role
        is not a superuser and `pg_control_*` is refused; that must degrade to "no cluster evidence"
        and never fail boot, so the remaining detection (counter versus journal) still applies.
        """
        try:
            cur.execute('SELECT (SELECT system_identifier FROM pg_control_system()), '
                        '       (SELECT timeline_id FROM pg_control_checkpoint())')
            system_identifier, timeline_id = cur.fetchone()
            return f'{system_identifier}/{timeline_id}'
        except psycopg.Error as exc:
            # Not an error: one INFO line, once per boot, so the fallback is visible in a log
            # rather than being mistaken for a check that ran and passed.
            logger.info('cluster fingerprint unavailable (%s) — epoch detection falls back to the '
                        'journal comparison plus the restore runbook', exc.__class__.__name__)
            cur.connection.rollback()
            return None
