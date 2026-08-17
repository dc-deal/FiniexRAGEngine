-- 007_source_quarantine_log — the quarantine episode history, and the ladder's own state
-- (ISSUE_84).
--
-- ISSUE_11 gave every failing feed a flat 24h quarantine after 5 consecutive failures. Two
-- production incidents priced that policy: on 2026-08-15 ecb_press (58,227 polls, 99.97% success)
-- had 3m42s of TLS timeouts and lost a full day of ingest — ~1,900 polls never made, 19 envelopes
-- marked partial. On 2026-07-29 a ~5h host outage put all twelve feeds over the threshold within
-- the same minutes, so every one of them was quarantined for 24h and a five-hour outage became a
-- ~25h blackout, during which the engine kept producing and kept paying on a corpus draining from
-- 71 to 33 relevant articles per pass.
--
-- The replacement is the circuit-breaker shape BudgetGuard already implements for paid calls:
-- a graduated cool-off ladder, one half-open probe at expiry, and a guard that recognises a
-- fleet-wide failure as a local problem rather than twelve feed problems.
--
-- WHY A TABLE AND NOT A COUNTER ON source_health:
-- the rung is derived from this history (a COUNT over `ladder_reset_hours`), so the number that
-- decides the cool-off and the rows that explain it can never disagree. A denormalised
-- `episodes` column would be a second truth that drifts after the first bugfix.
--
-- WHY THE CORRELATED EVENT LIVES HERE TOO (kind='correlated', source_id NULL):
-- it explains a *gap* in an individual feed's history. Without it 2026-07-29 reads as "failed
-- five times, nothing happened", and nobody can tell whether the policy worked or failed. In a
-- separate table, understanding one feed's story would mean reading two histories side by side.
--
-- RETENTION: none, deliberately — unlike source_poll_log (11 MB/day, 14 days). At ~1 KB per
-- episode and 5-20 episodes/week across twelve feeds this is ~1 MB/year, about one thousandth of
-- the journal. That is what makes `timeline` worth its bytes: the journal's 14-day window
-- truncates the minute-by-minute view, so the poll lines that triggered a decision are frozen
-- into the episode at decision time and outlive it.

CREATE TABLE IF NOT EXISTS source_quarantine_log (
    id             BIGSERIAL PRIMARY KEY,
    kind           TEXT NOT NULL,          -- quarantine | correlated
    source_id      TEXT,                   -- NULL for a correlated (set-level) event
    source_set     TEXT NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL,
    ended_at       TIMESTAMPTZ,            -- NULL while the episode is still running
    rung           INT,                    -- 0-based index into the ladder; NULL when correlated
    rungs_total    INT,                    -- ladder length at decision time (renders as "1/3")
    cooloff_hours  REAL,
    trigger_type   TEXT,                   -- error_type at the moment of the decision
    trigger_status INT,                    -- HTTP status where the source knew one
    trigger_ms     REAL,                   -- that failure's measured duration — picks the rung
    streak         INT,                    -- consecutive failures when the decision was taken
    failed_of      TEXT,                   -- '12/12' on a correlated event, else NULL
    outcome        TEXT,                   -- probe_ok | escalated | resumed | manual_clear
    timeline       JSONB NOT NULL DEFAULT '[]',  -- frozen poll lines that led to the decision
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The history report reads per feed (newest first); the ladder counts a source's recent episodes;
-- the correlated events are read per set. One index per access path.
CREATE INDEX IF NOT EXISTS source_quarantine_log_source ON source_quarantine_log (source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS source_quarantine_log_set    ON source_quarantine_log (source_set, started_at DESC);
