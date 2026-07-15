"""Persistence for pipeline outcomes — the source of truth for backtest replay (ISSUE_8)."""
import json
import logging
from typing import Any, Dict, Optional

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.outcome_types import AnalysisEnvelope, SentimentEnvelope

logger = logging.getLogger(__name__)


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
        self._ensure_schema()

    def _connect(self) -> psycopg.Connection:
        try:
            return psycopg.connect(self._database_url)
        except psycopg.Error as exc:
            raise VectorStoreError(f'cannot connect to the outcome store: {exc}') from exc

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    f'CREATE TABLE IF NOT EXISTS {self._table} ('
                    'id BIGSERIAL PRIMARY KEY, '
                    'pipeline_id TEXT NOT NULL, '
                    'ts TIMESTAMPTZ NOT NULL, '        # envelope.timestamp (analysis time)
                    'status TEXT NOT NULL, '           # success | partial | error
                    'envelope JSONB NOT NULL, '        # the exact served JSON (source of truth)
                    'raw_output JSONB)')               # ISSUE_36: {symbol: raw scored dict}
                # The /latest read path: newest row per pipeline via one index walk.
                cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{self._table}_latest '
                            f'ON {self._table} (pipeline_id, ts DESC)')
        except psycopg.Error as exc:
            raise VectorStoreError(f'outcome-store schema init failed: {exc}') from exc

    def save(self, envelope: AnalysisEnvelope,
             raw_output: Optional[Dict[str, Any]] = None) -> None:
        """Persist one envelope (+ the per-symbol raw LLM output next to it)."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
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
