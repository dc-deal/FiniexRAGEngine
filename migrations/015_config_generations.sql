-- 015_config_generations — WHEN a configuration was live, because two edge points are not a span
-- (ISSUE_116).
--
-- `config_fingerprints` (migration 005) holds one row per distinct configuration with `first_seen`
-- and `last_seen`. That answers *what did this fingerprint stand for* and cannot answer *when was it
-- live*, and the difference is not academic: on 2026-08-27 two fingerprints on the crypto stream
-- appeared to overlap, because B's `first_seen` sat inside A's span and ran to the present. They
-- never overlapped. Five restarts happened around one deploy and B produced exactly ONE pass before
-- being reverted. Refuting the apparent overlap took `seq` monotonicity, a per-envelope count and
-- the second stream as a control — three arguments reconstructed by hand for a question a table
-- should answer. And the reverted single-pass excursion is invisible to every ordering check there
-- is: only the gaps show it.
--
-- Re-activation is what the registry cannot represent. It upserts, so A → B → A moves only A's
-- `last_seen`: A then reads t0..t3 and B reads t1..t2, i.e. B nested inside A, and the documented
-- read (`ORDER BY first_seen`) renders them A-then-B — hiding that A was current after B. The boot
-- line cannot rescue it either: `(new)` comes from the registry's insert/update boolean and is
-- therefore silent exactly on a rollback.
--
-- So: one row per ACTIVATION, append-only, never updated. Deliberately a second table rather than
-- columns on the first, because the cardinalities differ — `config_fingerprints` is one row per
-- configuration (payload, deduplicated), this is one row per activation (timeline, never
-- deduplicated). Merging them would either lose activations or repeat the payload per activation.
--
-- No retention, for the same reason as 005: it must outlive the archive it explains.

CREATE TABLE IF NOT EXISTS config_generations (
    id                 BIGSERIAL PRIMARY KEY,  -- a log, not a series: gaps here are irrelevant
    fingerprint        TEXT NOT NULL,          -- the generation activated (see note on the FK)
    pipeline_id        TEXT NOT NULL,          -- per stream: streams activate independently
    activated_at       TIMESTAMPTZ NOT NULL,   -- when it began producing on this stream
    reason             TEXT NOT NULL,          -- boot | reload | rollback (GENERATION_REASONS)
    process_started_at TIMESTAMPTZ NOT NULL    -- which process did it: five boots stay five rows
);

-- The read this table exists for: one stream's activations, newest first.
CREATE INDEX IF NOT EXISTS idx_config_generations_stream
    ON config_generations (pipeline_id, activated_at DESC);

-- NOTE on `fingerprint`: deliberately NOT a foreign key into `config_fingerprints`. That write is
-- best-effort by design (a registry error is logged and swallowed, because losing an explanation
-- must never fail a boot), so a constraint would turn a missing explanation into a missing
-- activation — the opposite of what this table is for.
--
-- NOTE on `reason`: strict at the producing seam (`GenerationReason` + `GENERATION_REASONS`), plain
-- TEXT here, per the closed-vocabulary rule — a row written by a later version carrying a value this
-- one does not know must still load. `reload` has no writer until ISSUE_115; it is in the vocabulary
-- from the start because the activation log is what makes that issue's rollback provable.
