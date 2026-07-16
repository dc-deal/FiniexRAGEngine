-- 001_init — the schema as it stands today (ISSUE_14).
--
-- This file is the BASELINE, and it is deliberately idempotent (`IF NOT EXISTS` everywhere):
-- it must be a complete no-op against the already-populated database it was extracted from,
-- while building the full schema on a fresh one. That single property removes the need for a
-- separate "stamp an existing DB as migrated" mechanism.
--
-- It is the ONLY migration allowed to look like this. From 002 onward the runner guarantees
-- each file runs exactly once, so migrations are plain forward DDL — no `IF NOT EXISTS` crutch.
--
-- Retires the five former `_ensure_schema()` bodies and the four inline
-- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements (which used to re-run on every boot).

CREATE EXTENSION IF NOT EXISTS vector;

-- --- corpus: the shared article store (ISSUE_3) --------------------------------------------
-- `embedding vector(1536)` is stated literally, not templated from config: a corpus is bound to
-- ONE embedding model (ISSUE_16), and its boot guard refuses a mismatch. Changing the model is
-- therefore a deliberate re-embed migration — never a config flip — so the dimension is a fact
-- of the schema, not a parameter.
CREATE TABLE IF NOT EXISTS articles (
    article_id          TEXT PRIMARY KEY,
    source_id           TEXT NOT NULL,
    source_weight       REAL NOT NULL,
    url                 TEXT NOT NULL,
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL,
    language            TEXT NOT NULL,
    published_at        TIMESTAMPTZ NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL,
    embedding           vector(1536) NOT NULL,
    -- Breaking detection (ISSUE_11): graded importance tier + the candidate flag the eval
    -- wake reads; flagged_at is the detection timestamp (reaction time = flagged_at - fetched_at).
    importance          SMALLINT,
    breaking_candidate  BOOLEAN NOT NULL DEFAULT FALSE,
    flagged_at          TIMESTAMPTZ
);

-- No ANN index on `embedding` yet: cosine search is an exact full scan, which is fine at the
-- current corpus size. Add an HNSW index (vector_cosine_ops) here before the scan dominates.

-- --- corpus guard stamp (ISSUE_16) ---------------------------------------------------------
-- Binds a corpus table to the embedding model that built it. The engine reads this at boot and
-- refuses to start on a mismatch, so a config edit can never silently poison the corpus.
CREATE TABLE IF NOT EXISTS corpus_meta (
    table_name          TEXT PRIMARY KEY,
    embedding_model     TEXT NOT NULL,
    dimensions          INTEGER NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --- outcome store: the produced envelopes (ISSUE_8) ---------------------------------------
-- The source of truth for /latest and the metrics warehouse the reports aggregate over.
CREATE TABLE IF NOT EXISTS outcomes (
    id                  BIGSERIAL PRIMARY KEY,
    pipeline_id         TEXT NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,   -- envelope.timestamp (analysis time)
    status              TEXT NOT NULL,          -- success | partial | error
    envelope            JSONB NOT NULL,         -- the exact served JSON
    raw_output          JSONB                   -- ISSUE_36: {symbol: raw scored dict}
);

-- The /latest read path: newest row per pipeline via one index walk.
CREATE INDEX IF NOT EXISTS idx_outcomes_latest ON outcomes (pipeline_id, ts DESC);

-- --- billing log: one row per paid API call (ISSUE_23/32) ----------------------------------
-- Token usage is irreconstructable after the call, so it is captured at the call; usd_cost is
-- frozen at record time from the config price table. duration_ms makes each row a latency sample.
CREATE TABLE IF NOT EXISTS cost_log (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    section             TEXT NOT NULL,          -- ingest_news | ingest_query | llm_eval | ...
    model               TEXT NOT NULL,
    prompt_tokens       INTEGER NOT NULL,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL,
    usd_cost            DOUBLE PRECISION NOT NULL,
    pipeline_id         TEXT,
    duration_ms         DOUBLE PRECISION,       -- API-call latency (ISSUE_32)
    model_snapshot      TEXT                    -- the served model (response.model, ISSUE_40)
);

-- --- query-vector cache (ISSUE_19) ---------------------------------------------------------
-- The fixed symbol queries are embedded once; the key carries the model + dimensions so a model
-- change busts the cache instead of returning vectors from another map.
CREATE TABLE IF NOT EXISTS query_vectors (
    query_text          TEXT NOT NULL,
    embedding_model     TEXT NOT NULL,
    dimensions          INTEGER NOT NULL,
    embedding           vector(1536) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (query_text, embedding_model, dimensions)
);

-- --- source health: one rolling row per feed (ISSUE_49) ------------------------------------
-- Identity is the config source_id (joins articles.source_id; one row = one poller). The
-- normalized host groups the same feed appearing under different source-sets.
CREATE TABLE IF NOT EXISTS source_health (
    source_id             TEXT PRIMARY KEY,
    host                  TEXT,
    source_set            TEXT,
    total_polls           BIGINT NOT NULL DEFAULT 0,
    total_success         BIGINT NOT NULL DEFAULT 0,
    total_failures        BIGINT NOT NULL DEFAULT 0,
    consecutive_failures  INT NOT NULL DEFAULT 0,
    last_success_at       TIMESTAMPTZ,
    last_failure_at       TIMESTAMPTZ,
    last_status           INT,
    last_error_type       TEXT,
    flagged               BOOLEAN NOT NULL DEFAULT FALSE,
    flagged_at            TIMESTAMPTZ,
    quarantined_until     TIMESTAMPTZ,
    recent_events         JSONB NOT NULL DEFAULT '[]',
    first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
