"""Persistence for pipeline outcomes — the source of truth for backtest replay (ISSUE_8)."""
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg

from finiexragengine.exceptions.ragengine_errors import FiniexRagError, VectorStoreError
from finiexragengine.core.outcome.episode_registry import EpisodeRegistry
from finiexragengine.core.outcome.stream_sequencer import StreamSequencer
from finiexragengine.types.eval_types import EpisodeUpsert
from finiexragengine.types.outcome_types import AnalysisEnvelope, SentimentEnvelope
from finiexragengine.types.stream_types import StreamHead

logger = logging.getLogger(__name__)

# Distinguishes "not looked up yet" from "looked up and unavailable" — the second is a real answer
# and must not trigger a retry on every health poll.
_UNRESOLVED = object()

# See `_connect`. Five seconds against a measured ~6 ms healthy connect.
_CONNECT_TIMEOUT_SECONDS = 5


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

    def __init__(self, database_url: str, table: str = 'outcomes',
                 notify_channel: str = 'finiex_outcomes') -> None:
        self._database_url = database_url
        self._table = table
        # The LISTEN/NOTIFY channel the stream dispatcher tails (ISSUE_9). Defaulted rather than
        # required because every existing caller writes envelopes without caring about the stream —
        # and a mismatched channel is a stream that never advances, so the tracked config carries
        # the same literal and `create_app` passes it explicitly.
        self._notify_channel = notify_channel
        # Not an injected collaborator: minting is part of how this store writes, not a strategy it
        # picks. The sequencer holds no state of its own — `reconcile()` at boot may use its own
        # instance without coordination.
        self._sequencer = StreamSequencer(database_url, outcomes_table=table)
        # Same reasoning as the sequencer: recording an episode is part of how this store
        # writes, and it has to share the envelope's transaction (ISSUE_65).
        self._episodes = EpisodeRegistry()
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
        """A connection per call, and the connect is **bounded** (ISSUE_9 follow-up).

        `socket.setdefaulttimeout` cannot bound it — libpq is C-level and ignores it — so an
        un-timeouted connect here is the ISSUE_73 shape in a new place: one call that never returns,
        on a path that runs unattended for weeks. This store sits on the serving path (`/latest`),
        inside every pass, and inside the stream's tail; a healthy local connect measures ~6 ms, so
        five seconds is three orders of magnitude of headroom and still fails fast when the database
        is genuinely gone.

        Deliberately NOT a global fix: 34 call sites in this package connect without a bound. The two
        on the stream's path are closed here because that is where the tail hangs; the rest is its own
        issue, because a report CLI hangs in front of an operator who can interrupt it, and a worker
        loop does not.
        """
        try:
            return psycopg.connect(self._database_url,
                                   connect_timeout=_CONNECT_TIMEOUT_SECONDS)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the outcome store: {exc}') from exc

    def save(self, envelope: AnalysisEnvelope,
             raw_output: Optional[Dict[str, Any]] = None,
             episodes: Optional[List[EpisodeUpsert]] = None) -> None:
        """Stamp the envelope with its stream position, then persist it (+ the raw LLM output).

        The stamp is minted **inside this transaction** and written **into the envelope** before it
        is serialized. Both matter (ISSUE_9):

        * inside the transaction, because that is what makes `seq` gapless — the counter's row lock
          is held to COMMIT, so a rollback returns the number instead of burning it;
        * into the envelope, because the JSONB column is the exact served JSON. A `seq` living only
          in a table column would never reach the archive: `OutcomeExporter` reads the envelope.

        `episodes` are the breaking-episode rows this pass touched (ISSUE_65). They are written in
        the same transaction for the same reason the stamp is: the envelope already carries
        `breaking_episode_id`, and a journal row referencing an episode the registry never received
        would be a dangling identity — the one shape a correlation key must not have.

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
                for episode in episodes or ():
                    self._episodes.upsert(cur, episode)
                # The stream's wake-up, INSIDE this transaction (ISSUE_9 §3.4). PostgreSQL delivers
                # notifications on COMMIT, which is exactly the semantics the dispatcher needs: it
                # is woken only for rows it can actually read, and a rolled-back pass notifies
                # nobody. `pg_notify()` rather than the `NOTIFY` statement because the channel is
                # configuration and this form takes it as a parameter instead of interpolating a
                # name into SQL.
                #
                # The payload is the `pipeline_id` alone — the dispatcher re-reads the journal
                # forward by `seq` regardless, so a payload carrying the envelope would be a second
                # copy of the frame on a channel with an 8000-byte limit.
                cur.execute('SELECT pg_notify(%s, %s)',
                            (self._notify_channel, envelope.pipeline_id))
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

    # --- the stream's reads (ISSUE_9) -----------------------------------------------------------

    def stream_head(self, pipeline_id: str) -> StreamHead:
        """Where this stream currently stands: `(seq, epoch, available_msc)`.

        Read from `stream_seq`, not from the journal, and that is the cheaper *and* the more correct
        source: the counter is updated inside the envelope's own transaction, so a rolled-back pass
        leaves it untouched and its value always equals the journal's highest committed `seq`. One
        indexed point read instead of an aggregate over JSONB.

        A stream the sequencer has never seen returns `(0, 0, None)` — which the caller renders as a
        cold start. `seq: 0` cannot collide with a real position, because the counter returns
        `seq + 1`.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute('SELECT seq, epoch, last_available_msc FROM stream_seq '
                            'WHERE pipeline_id = %s', (pipeline_id,))
                row = cur.fetchone()
        except psycopg.Error as exc:
            raise VectorStoreError(f'stream head read failed: {exc}') from exc
        if row is None:
            return StreamHead(seq=0, epoch=0, available_msc=None)
        return StreamHead(seq=int(row[0]), epoch=int(row[1]), available_msc=row[2])

    def envelopes_by_seq(self, pipeline_id: str, after_seq: int,
                         limit: int) -> List[Dict[str, Any]]:
        """This stream's envelopes with `seq > after_seq`, ascending, at most `limit` of them.

        **Returns the raw JSONB rows, deliberately not `SentimentEnvelope` instances.** The frame is
        the stored envelope verbatim (§3.2); validating into a model only to serialize it again would
        let a model default rewrite an archived line on its way to the wire — and the parity anchor
        ("pushed equals stored, byte for byte") would then be a claim about the model rather than
        about the store.

        Ascending is the contract, not a convenience: the dispatcher advances by `seq` and wire order
        must equal `seq` order. `envelope->>'seq'` is covered by the `outcomes_stream_seq` expression
        index, which PostgreSQL scans backwards for an ascending walk.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"SELECT envelope FROM {self._table} "
                    "WHERE pipeline_id = %s AND (envelope->>'seq')::BIGINT > %s "
                    "ORDER BY (envelope->>'seq')::BIGINT ASC LIMIT %s",
                    (pipeline_id, after_seq, limit))
                rows = cur.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f'stream read failed: {exc}') from exc
        return [row[0] for row in rows]

    def oldest_seq_since(self, pipeline_id: str, cutoff: datetime) -> Optional[int]:
        """The lowest `seq` this stream produced at or after `cutoff` — the replay window's floor.

        `None` when the stream produced nothing inside the window, which is a real state (a quiet
        weekend on a low-cadence pipeline) and not an error.

        Bounded on `ts`, the indexed column, rather than on the JSONB `seq`: the window is a time
        question and the answer is a position, so the conversion belongs here — a caller comparing
        seq numbers to hours would be inventing a cadence.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f"SELECT min((envelope->>'seq')::BIGINT) FROM {self._table} "
                    "WHERE pipeline_id = %s AND ts >= %s AND envelope->>'seq' IS NOT NULL",
                    (pipeline_id, cutoff))
                row = cur.fetchone()
        except psycopg.Error as exc:
            raise VectorStoreError(f'stream window read failed: {exc}') from exc
        return int(row[0]) if row and row[0] is not None else None

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
