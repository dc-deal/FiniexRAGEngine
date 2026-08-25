-- 010_breaking_episodes — episode identity as a first-class row (ISSUE_65).
--
-- A breaking episode is an EVENT inside a stream of ordinary passes, and until now it existed only
-- as a per-pass boolean plus a grouping re-derived at read time. `breaking_episode_id` on the
-- envelope gives the consumer identity; this table gives the episode a home on our side.
--
-- What it is FOR, precisely, because the original design overstated it: it is not what makes the
-- assignment restart-safe. ISSUE_82 already solved that by seeding `BreakingEpisodeRule` from the
-- persisted envelopes at boot, so a process that restarts mid-story rejoins the open episode with
-- no help from this table. What the table adds is episode-level aggregates as plain SQL — count,
-- mean reaction, duration, passes per episode — which today cost a full JSONB scan of `outcomes`
-- and a re-grouping in Python.
--
-- `episode_id` is the primary key and is the same composite the envelope carries
-- (`<pipeline_id>:<episode_key>:<started_at>`), so the unique index `ON CONFLICT` needs exists by
-- construction rather than as a second declaration that could drift from the id's shape.
--
-- The write is `INSERT ... ON CONFLICT DO UPDATE`, never read-check-insert. Since ISSUE_74 removed
-- the shared pass lock, two eval workers run genuinely concurrently — the ISSUE_42 model variants
-- score the same symbols under different pipeline_ids, and a future consumer of this table should
-- not have to rediscover why a read-modify-write across the database produces either duplicate
-- rows or a constraint error here.
--
-- `episode_key` is stored beside `symbol` on purpose: since ISSUE_82 the grouping key is the
-- RETRIEVAL QUERY, not the ticker. One episode can therefore span several symbols (ETHUSD/ETHEUR
-- under one analysis, ISSUE_70) — `symbol` names the row that opened it, `episode_key` names what
-- it actually groups.
--
-- There is deliberately no `ended` column. Whether an episode is over is
-- `last_seen_at + episode_gap_minutes < now()`, and that gap is per-pipeline CONFIG: writing the
-- derived state here would freeze one policy value into history, so retuning the gap would leave
-- the archive asserting something the rule no longer agrees with. Every surface derives it, the
-- same way the live display and the store report already do.

CREATE TABLE IF NOT EXISTS breaking_episodes (
    episode_id      TEXT PRIMARY KEY,       -- <pipeline_id>:<episode_key>:<started_at ISO, seconds>
    pipeline_id     TEXT        NOT NULL,
    episode_key     TEXT        NOT NULL,   -- the retrieval query the episode groups by
    symbol          TEXT        NOT NULL,   -- the symbol whose row opened it
    signal          TEXT        NOT NULL,   -- frozen at the opening pass, like the reaction times
    started_at      TIMESTAMPTZ NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL,   -- advanced by every pass inside the episode
    n_passes        INTEGER     NOT NULL DEFAULT 1,
    urgency         DOUBLE PRECISION,       -- the opening pass's score
    engine_s        DOUBLE PRECISION,       -- envelope ts - freshest source fetched_at (ISSUE_81)
    end_to_end_s    DOUBLE PRECISION,       -- envelope ts - freshest REAL published_at
    reason          TEXT        NOT NULL DEFAULT '',   -- the opening pass's `reasoning`
    breaking_reason TEXT        NOT NULL DEFAULT '',   -- the model's purpose-built line (ISSUE_64)
    prompt_version  TEXT        NOT NULL DEFAULT ''    -- scores are only comparable within one
);

-- The report read: episodes of one pipeline, newest first.
CREATE INDEX IF NOT EXISTS breaking_episodes_by_pipeline
    ON breaking_episodes (pipeline_id, started_at DESC);
