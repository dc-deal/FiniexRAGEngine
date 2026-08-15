-- 004_source_poll_log — one row per feed poll attempt, so feed questions stop being guesses
-- (ISSUE_76).
--
-- ISSUE_73 gave the fetch a hand-picked 10s timeout and nothing to judge it by. On 2026-08-15
-- ecb_press — 58,227 polls at 99.97% success — hit TLS handshake timeouts and was quarantined for
-- 24 hours after 3m42s of consecutive failure. Two questions could not be answered from anything
-- the engine had stored: was the feed *slow* (would 20s have worked?) or *dead*, and was a 24-hour
-- blackout proportionate to a four-minute wobble. A timed-out fetch left no trace of how long it
-- took, and outage durations were nowhere recorded.
--
-- This is cost_log's shape applied to the unpaid calls: one journal row per attempt, reported as a
-- windowed aggregate via native percentile_cont (see reports/source_latency_report.py). A journal
-- rather than fixed histogram buckets, because at this scale it also answers the questions we have
-- not thought of yet — which is the whole point of the issue.
--
-- `duration_ms` is captured on BOTH paths. That is the load-bearing column: StageTimer records
-- nothing for a stage that raises, so exactly the polls worth studying produced no data before.
--
-- Only real attempts are journaled. A floor-skip or a quarantine skip gets no row — at the 15s
-- worker tick they would add ~70k rows/day of noise. Absence carries that signal better: an outage
-- IS a gap in a source's poll series, and the polls a quarantine cost are the gap divided by that
-- source's own median inter-poll interval. A gap also catches worker death, config changes and
-- poll-floor changes, which a quarantine-only row never would.
--
-- Unlike cost_log (the billing warehouse, kept forever) this is diagnostic and carries a retention
-- window — `diagnostics.poll_log_retention_days`, pruned once per UTC day by the writer.

CREATE TABLE IF NOT EXISTS source_poll_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,   -- when the attempt started
    source_id   TEXT NOT NULL,          -- joins to source_health.source_id / articles.source_id
    source_set  TEXT NOT NULL,
    outcome     TEXT NOT NULL,          -- ok | failed (what the poll itself did — see PollOutcome)
    duration_ms REAL,                   -- measured on success AND failure; NULL only if unmeasured
    error_type  TEXT,                   -- RunError taxonomy on failure, NULL on success
    status      INT,                    -- HTTP status where the source knows one
    articles    INT NOT NULL DEFAULT 0  -- articles the fetch returned (0 on a 304)
);

-- The report reads per source over a time window (percentiles, gap detection); the prune deletes
-- by age across all sources. One index for each access path.
CREATE INDEX IF NOT EXISTS source_poll_log_source_ts ON source_poll_log (source_id, ts DESC);
CREATE INDEX IF NOT EXISTS source_poll_log_ts        ON source_poll_log (ts);
