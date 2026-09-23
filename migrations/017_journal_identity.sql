-- 017_journal_identity — WHICH deployment produced a row, when one cluster carries several
-- (ISSUE_9 follow-up).
--
-- `/v1/health` already reports `journal_id`: a 12-char fingerprint of PostgreSQL's
-- `system_identifier`, derived and therefore unfalsifiable. It identifies the **cluster**, and that
-- is one level too coarse for the question a consumer actually has. The suite creates a
-- `finiex_test` schema inside the production database at every version bump (docs/testing.md), and
-- a second deployment against the same cluster is one schema away — both answer with production's
-- `journal_id`, so two series merged from them cannot be told apart afterwards.
--
-- So: an identity **per schema**, because a schema is what a deployment actually owns. Stamped onto
-- every envelope as `instance_id`, served at `/v1/health` beside `journal_id`.
--
-- **Minted here and never by the engine — one deployment, one edge.** A process minting at boot
-- would produce a new identity at every restart, and a consumer merging on this field would read a
-- discontinuity where nothing happened. Applying a migration is an operator action (the migrate CLI
-- alone applies; `schema_guard` only checks), so an edge in this field is always one somebody
-- caused. A re-mint is a deliberate UPDATE — documented in docs/development/diagnostics.md — and
-- the discontinuity it produces IS the point: it is how a restore into a new deployment says "this
-- is a different producer".
--
-- **Random, not derived**, which is the opposite choice from `journal_id` and deliberate. A value
-- derived from the cluster identifier plus the schema name would be reproducible — and therefore
-- impossible to re-mint: dropping a schema and recreating it for a genuinely new deployment would
-- hand back the id the old one had. Derivation is right where the thing identified cannot be
-- replaced (a cluster); minting is right where it can (a deployment).
--
-- **No retroactive stamping.** Rows written before this carry no `instance_id`, and absent means
-- "produced before this existed" — the same reading as every other provenance field in the
-- envelope, never "same as the neighbour". The consumer's boundary is the first stamped `seq` per
-- stream, which is a fact in their own data rather than a date they have to be told.
--
-- No retention: it explains envelopes that are themselves permanent.

CREATE TABLE IF NOT EXISTS journal_identity (
    -- One row, enforced by the database rather than by convention: two identities for one schema
    -- would be worse than none, because both would look authoritative and neither would be wrong
    -- on its face. The CHECK pins the key to TRUE, so the primary key admits exactly one row.
    singleton   BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    -- The wire format, checked where the value is written. 12 lowercase hex is what the consumer
    -- parses; a hand re-mint pasting a full UUID is refused here instead of reaching an archive and
    -- being discovered by their loader.
    instance_id TEXT NOT NULL CHECK (instance_id ~ '^[0-9a-f]{12}$'),
    -- When this deployment began. Not a column the engine reads — it is what makes a re-mint
    -- readable afterwards, next to the first envelope that carries the new id.
    minted_at   TIMESTAMPTZ NOT NULL
);

-- The mint itself. `gen_random_uuid()` is core PostgreSQL since 13 (this engine runs 16), so no
-- extension is required — `pgcrypto` for `gen_random_bytes` would have been a dependency for six
-- bytes of randomness.
--
-- `ON CONFLICT DO NOTHING` although the runner applies each file exactly once: the statement is
-- also what somebody runs by hand when they copy it out of here, and minting twice is precisely the
-- edge this table exists to make impossible.
INSERT INTO journal_identity (singleton, instance_id, minted_at)
SELECT TRUE, left(replace(gen_random_uuid()::text, '-', ''), 12), now()
ON CONFLICT (singleton) DO NOTHING;
