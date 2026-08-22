"""Persistence for pipeline outcomes — the source of truth for backtest replay (ISSUE_8)."""
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg

from finiexragengine.exceptions.ragengine_errors import FiniexRagError, VectorStoreError
from finiexragengine.core.outcome.stream_sequencer import StreamSequencer
from finiexragengine.types.outcome_types import AnalysisEnvelope, SentimentEnvelope

logger = logging.getLogger(__name__)

# Distinguishes "not looked up yet" from "looked up and unavailable" — the second is a real answer
# and must not trigger a retry on every health poll.
_UNRESOLVED = object()


class OutcomeStore:
    """Stores every produced envelope and serves the latest per pipeline.

    The store — not the live socket — is the source of truth: every outcome
    (breaking or not, success or error) is persisted so a backtest can replay it
    deterministically and error statistics aggregate from persisted envelopes'
    `status`/`errors`, never from log text.

    Backing store: a Postgres table alongside pgvector (same database, one
    infrastructure) — `/latest` is an indexed point read, the metrics warehouse
    stays queryable in SQL. JSONL is deliberately **not** the store: that is the
    *collector's* downstream archive format (ISSUE_9); the operational store and
    the export artifact are different layers.

    Row shape: the envelope itself is one JSONB column (the exact served JSON —
    what you persist is what a consumer parses), plus three thin query columns
    (`pipeline_id`, `ts`, `status`) for the latest-read and status aggregation.
    The raw per-symbol LLM output (ISSUE_36) rides in its own JSONB column next
    to the envelope: same key, explicitly non-load-bearing (free to evolve, never
    bumps `schema_version`) — with the prompt fingerprint (ISSUE_33) and the
    served model snapshot already inside the envelope, a persisted run is fully
    reconstructable.
    """

    def __init__(self, database_url: str, table: str = 'outcomes') -> None:
        self._database_url = database_url
        self._table = table
        # Not an injected collaborator: minting is part of how this store writes, not a strategy it
        # picks. The sequencer holds no state of its own — `reconcile()` at boot may use its own
        # instance without coordination.
        self._sequencer = StreamSequencer(database_url, outcomes_table=table)
        self._journal_id: Any = _UNRESOLVED     # resolved on first read, then cached

    def get_sequencer(self) -> StreamSequencer:
        return self._sequencer

    def journal_id(self) -> Optional[str]:
        """A stable fingerprint of the journal this store writes into (ISSUE_9).

        Two engines pointing at one database are one series; one engine pointed at a different
        database is a different series, whatever else it has in common. So the identity that matters
        to a consumer is the **store's**, not the process's — hence the name.

        Derived from PostgreSQL's `system_identifier`, never configured. A declared label
        (`environment: 'production'`) would be a claim, and a mislabelled development instance is
        worse than no label at all: it makes a rehearsal look like proof. The consumer asked for this
        because their release certificate has to record which producer it was taken against, and an
        unfalsifiable certificate is the artifact it exists to prevent.

        `None` when the identifier cannot be read — on a managed Postgres the engine's role is not a
        superuser and `pg_control_system()` is refused. "Cannot be established here" is the honest
        answer; deriving a substitute from the DSN would collide, because a dev and a server
        deployment can easily share `host:port/database`.

        Cached: the health endpoint is polled, and this cannot change without a restart.
        """
        if self._journal_id is not _UNRESOLVED:
            return self._journal_id
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute('SELECT system_identifier FROM pg_control_system()')
                identifier = str(cur.fetchone()[0])
            self._journal_id = hashlib.sha256(identifier.encode('utf-8')).hexdigest()[:12]
        except (psycopg.Error, FiniexRagError) as exc:
            logger.info('journal identity unavailable (%s) — /health reports it as absent',
                        exc.__class__.__name__)
            self._journal_id = None
        return self._journal_id

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the outcome store: {exc}') from exc

    def save(self, envelope: AnalysisEnvelope,
             raw_output: Optional[Dict[str, Any]] = None) -> None:
        """Stamp the envelope with its stream position, then persist it (+ the raw LLM output).

        The stamp is minted **inside this transaction** and written **into the envelope** before it
        is serialized. Both matter (ISSUE_9):

        * inside the transaction, because that is what makes `seq` gapless — the counter's row lock
          is held to COMMIT, so a rollback returns the number instead of burning it;
        * into the envelope, because the JSONB column is the exact served JSON. A `seq` living only
          in a table column would never reach the archive: `OutcomeExporter` reads the envelope.

        `available_msc` is sampled here rather than at assembly on purpose — it means "the instant
        this became fetchable via /latest and pushable on the stream", which is the store write, not
        the end of the analysis. Precisely: it is when the write *began*, sampled after the
        connection is up so connect latency is not inside the claim. The remaining gap to the commit
        is the only way it can run early, and it is bounded by one insert.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                now_msc = int(datetime.now(timezone.utc).timestamp() * 1000)
                stamp = self._sequencer.mint(cur, envelope.pipeline_id, now_msc)
                envelope.seq = stamp.seq
                envelope.stream_epoch = stamp.epoch
                envelope.available_msc = stamp.available_msc
                envelope.available_msc_resyncs = stamp.resyncs
                envelope.available_msc_max_correction_ms = stamp.max_correction_ms
                cur.execute(
                    f'INSERT INTO {self._table} '
                    '(pipeline_id, ts, status, envelope, raw_output) '
                    'VALUES (%s, %s, %s, %s, %s)',
                    (envelope.pipeline_id, envelope.timestamp, envelope.status,
                     envelope.model_dump_json(),
                     json.dumps(raw_output) if raw_output else None))
        except psycopg.Error as exc:
            raise VectorStoreError(f'outcome save failed: {exc}') from exc

    def get_latest(self, pipeline_id: str) -> Optional[AnalysisEnvelope]:
        """The newest persisted envelope for a pipeline — None when nothing is stored."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT envelope FROM {self._table} '
                    'WHERE pipeline_id = %s ORDER BY ts DESC, id DESC LIMIT 1',
                    (pipeline_id,))
                row = cur.fetchone()
        except psycopg.Error as exc:
            raise VectorStoreError(f'outcome read failed: {exc}') from exc
        if row is None:
            return None
        # Validate back into the typed envelope — the store returns exactly what the
        # contract promises, not a raw dict. (Payload typing: sentiment is the only
        # outcome_type today; a second payload model dispatches on outcome_type here.)
        return SentimentEnvelope.model_validate(row[0])

    def get_since(self, pipeline_id: str, since: datetime) -> List[AnalysisEnvelope]:
        """One pipeline's envelopes from `since` onward, **oldest first** (ISSUE_82).

        The seeding read for the live breaking tracker: a restart used to start with empty episode
        state, so the boot pass re-opened an ongoing story as a fresh episode while the store report
        — re-deriving from these same rows — did not. Replaying the recent tail through the same
        rule makes the two agree across a restart instead of only within one process lifetime.

        Ascending order is the contract, not a convenience: `BreakingEpisodeRule` is driven in
        timestamp order. `status <> 'error'` mirrors the store report's filter, so both see the
        same population (an error envelope carries an empty `result` and could not hold an episode
        open anyway). One index walk on `(pipeline_id, ts DESC)`.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT envelope FROM {self._table} '
                    "WHERE pipeline_id = %s AND ts >= %s AND status <> 'error' "
                    'ORDER BY ts ASC, id ASC',
                    (pipeline_id, since))
                rows = cur.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f'outcome read failed: {exc}') from exc
        return [SentimentEnvelope.model_validate(row[0]) for row in rows]

    def get_latest_raw_output(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """The raw LLM output stored with the newest envelope (debug/replay, ISSUE_36)."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'SELECT raw_output FROM {self._table} '
                    'WHERE pipeline_id = %s ORDER BY ts DESC, id DESC LIMIT 1',
                    (pipeline_id,))
                row = cur.fetchone()
        except psycopg.Error as exc:
            raise VectorStoreError(f'outcome read failed: {exc}') from exc
        return row[0] if row else None
