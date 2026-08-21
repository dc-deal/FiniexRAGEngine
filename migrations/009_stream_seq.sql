-- 009_stream_seq — the per-stream sequence and epoch behind the output contract (ISSUE_9).
--
-- The consumer orders the signal series by `seq` and detects loss by finding a gap in it. Both
-- rest on one promise: **a gap means exactly one thing — a record that never arrived.** Nothing
-- else in the schema can make that promise.
--
-- `outcomes.id` (BIGSERIAL) cannot, for two independent reasons:
--   * it is ONE sequence shared by every stream. With crypto, forex and the ISSUE_42 model
--     variants writing into one table, a single stream's consumer reading that id sees
--     [1, 2, 3, 5, 8, 11, 14, ...] — measured on the journal: 86 gaps in 90 consecutive pairs,
--     every one of them another stream committing in between;
--   * PostgreSQL sequences are non-transactional by design. `nextval` is never rolled back, so a
--     failed insert burns a number permanently (verified on 16.14: committed 1, rolled back 2,
--     next committed 3).
--
-- A counter row solves both. `UPDATE ... RETURNING` inside the envelope's own transaction takes a
-- row lock held to COMMIT, which serialises the tail of every transaction on a stream: a rollback
-- returns the number, and mint order equals commit order, so the committed set is always a
-- contiguous prefix. The cost is one row lock per persist — at one pass per stream per ten
-- minutes, nothing.
--
-- `epoch` exists because `seq` CAN go backwards: a restore rewinds the counter, the engine
-- re-mints numbers the consumer already holds, and every new frame then sits below their cursor
-- and is silently ignored while the connection stays healthy. The cursor is (epoch, seq), so a
-- higher epoch reads as "resync", never as "below my mark". It is BIGINT and not a small counter
-- because the bump is MONOTONE, not an increment: `max(stored + 1, wall-clock seconds at bump)`.
-- A plain increment would be rewound by the very restore it is meant to signal, and the next bump
-- would REUSE the number — two different series both carrying epoch 2, which does not merely
-- break ordering, it collides the archive key (pipeline_id, stream_epoch, seq) and merges them.
--
-- `cluster_id` is the part that cannot live in the logical database's own bookkeeping: it caches
-- `<system_identifier>/<timeline_id>` from pg_control_system()/pg_control_checkpoint(). PITR and
-- standby promotion start a new timeline; a restore into a fresh cluster changes the system
-- identifier. A logical dump/restore in place changes neither — that shape stays a runbook step,
-- with the stream's `cursor_ahead` control frame as its detector.
--
-- `last_available_msc` and the two counters carry the monotonic clamp for `available_msc`, the one
-- stamp the engine samples from a wall clock (`collected_msc` on the export path is derived from
-- the envelope and cannot step backwards on its own). Named for the clock they describe: the
-- cross-collector contract's `anchor_*` stays reserved for a collector sampling its own clock,
-- because one field name meaning two things depending on who wrote it is the exact defect that
-- contract was written after.

CREATE TABLE IF NOT EXISTS stream_seq (
    pipeline_id                     TEXT PRIMARY KEY,       -- one row per stream (variants included)
    seq                             BIGINT      NOT NULL DEFAULT 0,  -- last minted; first envelope is 1
    epoch                           BIGINT      NOT NULL DEFAULT 1,  -- monotone; see the note above
    last_available_msc              BIGINT,                 -- clamp anchor; NULL before the first mint
    available_msc_resyncs           INTEGER     NOT NULL DEFAULT 0,  -- times the clock stepped back
    available_msc_max_correction_ms BIGINT      NOT NULL DEFAULT 0,  -- largest single correction held
    cluster_id                      TEXT,                   -- <system_identifier>/<timeline_id>
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reconciliation reads `max(seq)` per stream out of the journal at boot to catch a counter that
-- was reset while the journal survived. The envelope is JSONB, so without an expression index that
-- is a full scan of the table it is meant to protect.
CREATE INDEX IF NOT EXISTS outcomes_stream_seq
    ON outcomes (pipeline_id, ((envelope->>'seq')::BIGINT) DESC);
