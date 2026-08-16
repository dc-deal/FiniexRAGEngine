-- 005_config_fingerprints — what a `config_fingerprint` on an envelope actually stood for
-- (ISSUE_85).
--
-- The fingerprint itself is a scalar on the envelope: it says *that* the setup changed between
-- two archive days. It cannot say *what* changed — and the answer must not be "read the git
-- history of the engine repo", because that is precisely what a downstream consumer cannot do,
-- and because the running configuration is the merged one (tracked file + gitignored
-- user_configs/ overlay), which git does not have at all.
--
-- The IDE explicitly did not want a configuration sidecar shipped into the archive: the raw
-- JSONL already carries sources[] per signal plus metadata.sources_configured/sources_reached.
-- So the explanation stays here, on the engine's side of the seam — a small dimension table
-- next to the `outcomes` fact table, joined on demand by whoever investigates.
--
-- One row per DISTINCT setup (dozens over the project's life, not per pass), so it carries no
-- retention window: it must outlive the archive it explains. `last_seen` is boot-scoped — it
-- tracks assemblies, not passes, which is enough to lay a "this setup was active from … to …"
-- window against archive days; the per-pass record is the envelopes themselves.
--
-- `config` is JSONB rather than TEXT so a past setup can be queried and diffed
-- (config->'pipeline'->'retrieval', jsonb_pretty(config)). JSONB re-orders object keys on
-- storage, so the column is not byte-identical to the hashed string — it does not need to be:
-- the canonical form is a pure function of the value (sorted keys, sorted lists, fixed
-- separators), so re-canonicalizing this column reproduces the hashed string exactly.

CREATE TABLE IF NOT EXISTS config_fingerprints (
    fingerprint   TEXT PRIMARY KEY,      -- the 12-char sha256 prefix stamped on every envelope
    pipeline_id   TEXT NOT NULL,         -- the stream that FIRST registered this setup (see note)
    source_set_id TEXT NOT NULL,         -- the resolved set behind it, for readable browsing
    config        JSONB NOT NULL,        -- the canonical payload the hash was taken over
    first_seen    TIMESTAMPTZ NOT NULL,  -- first assembly that produced this fingerprint
    last_seen     TIMESTAMPTZ NOT NULL   -- most recent assembly (boot-scoped, not per pass)
);

-- NOTE on `pipeline_id`: the stream id is deliberately NOT part of the hash (identity is not an
-- input, and the envelope carries it anyway), so two streams configured identically map to the
-- same row — the column names whichever registered it first.

-- The one read pattern beyond a point lookup by primary key: "which setups existed, in order".
CREATE INDEX IF NOT EXISTS config_fingerprints_first_seen ON config_fingerprints (first_seen);
