"""Persisted breaking-episode identity (ISSUE_65) — the write half of `breaking_episodes`.

`upsert()` deliberately takes an **open cursor** instead of opening its own connection, for the same
reason `StreamSequencer.mint` does: it joins the envelope's insert transaction. That is what makes
the two halves of an episode's identity atomic — an envelope carrying `breaking_episode_id` and the
registry row for that id commit together or not at all, so the journal can never reference an
episode the registry has never heard of.

The statement is `INSERT ... ON CONFLICT DO UPDATE`, never read-check-insert. Since ISSUE_74 removed
the shared pass lock the eval workers run concurrently, and a read-modify-write across the database
would produce either duplicate rows or a constraint error under exactly the condition this table
exists to survive.

**What a continuation may change, and what it may not.** Only `last_seen_at` and `n_passes` advance.
The descriptive fields — signal, urgency, reason and both reaction times — are frozen at the opening
pass, because they describe *the edge*: re-sampling a reaction against an ageing article is the
defect ISSUE_81 removed from this metric, and it would come straight back if a continuation
overwrote them.
"""
import logging

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.eval_types import EpisodeUpsert

logger = logging.getLogger(__name__)


class EpisodeRegistry:
    """Records what the episode rule decided, inside the caller's transaction."""

    def __init__(self, table: str = 'breaking_episodes') -> None:
        self._table = table

    def upsert(self, cur: psycopg.Cursor, row: EpisodeUpsert) -> None:
        """Insert an opening episode, or advance one already running.

        `n_passes` is bumped only when this pass is genuinely newer than the row's `last_seen_at`,
        so a retried transaction replaying the same pass cannot inflate the count — the one place
        where a rollback-and-retry would otherwise be visible in the data.
        """
        try:
            cur.execute(
                f'INSERT INTO {self._table} '
                '(episode_id, pipeline_id, episode_key, symbol, signal, started_at, last_seen_at, '
                ' n_passes, urgency, engine_s, end_to_end_s, reason, breaking_reason, '
                ' prompt_version) '
                'VALUES (%(id)s, %(pid)s, %(key)s, %(sym)s, %(sig)s, %(start)s, %(seen)s, '
                '        1, %(urg)s, %(eng)s, %(e2e)s, %(reason)s, %(breaking)s, %(prompt)s) '
                'ON CONFLICT (episode_id) DO UPDATE SET '
                '  last_seen_at = GREATEST(EXCLUDED.last_seen_at, '
                f'                         {self._table}.last_seen_at), '
                f'  n_passes = {self._table}.n_passes + CASE '
                f'      WHEN EXCLUDED.last_seen_at > {self._table}.last_seen_at THEN 1 ELSE 0 END',
                {'id': row.episode_id, 'pid': row.pipeline_id, 'key': row.episode_key,
                 'sym': row.symbol, 'sig': row.signal, 'start': row.started_at,
                 'seen': row.last_seen_at, 'urg': row.urgency, 'eng': row.engine_s,
                 'e2e': row.end_to_end_s, 'reason': row.reason,
                 'breaking': row.breaking_reason, 'prompt': row.prompt_version})
        except psycopg.Error as exc:
            raise VectorStoreError(f'breaking episode not recorded: {exc}') from exc
