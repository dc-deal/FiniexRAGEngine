-- 008_resource_samples — what the engine costs the machine it runs on (ISSUE_89).
--
-- On 2026-08-01 the frozen production process was examined directly with py-spy and netstat. Two
-- numbers came out of it:
--
--     5 sockets in CLOSE_WAIT        the peer had closed, this process had not
--     1,191 MB resident memory      for a 4-worker process, high — but against what baseline?
--
-- Neither is explainable, and that is the finding. Both were read off a live process, neither was
-- recorded anywhere, and the restart took the measurement with it. A repeat occurrence would start
-- from the same blank sheet.
--
-- There is no leak left to hunt: `report_cli --send` genuinely leaked a TelegramClient and is
-- fixed, and the server path was already correct (one client per process, closed in the lifespan).
-- What remains is that the engine's design target is an unattended run of WEEKS, and nothing
-- measures whether it grows. 5 sockets in nine days is nothing; 5 per day for six weeks is a
-- file-descriptor ceiling whose failure mode looks exactly like twelve unreachable feeds.
--
-- WHY A TABLE AND NOT AN IN-MEMORY AGGREGATE:
-- carrying min/max/count/sum in the process would be free and wrong for this issue — a restart
-- wipes the series, and a restart is precisely the moment the history matters. The 1,191 MB
-- reading is already lost for exactly that reason, so an in-memory series would repeat the defect
-- it exists to fix.
--
-- VOLUME: one row per stall-watchdog tick (60s) = ~1.4k/day, ~10k/week. A rounding error next to
-- source_poll_log's ~51k PER DAY. Retention `diagnostics.resource_retention_days` (14), pruned
-- once per UTC day by the writer, so an incident and its resource history age out together.
--
-- Like source_poll_log and unlike source_health, this is DIAGNOSTIC: nothing in the engine's
-- behaviour depends on it, so a write error is logged and swallowed rather than raised.

CREATE TABLE IF NOT EXISTS resource_samples (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL,
    rss_mb        REAL NOT NULL,   -- resident set size of THIS process, not the database
    -- NULL when the platform refused the count: psutil needs privileges some hosts do not grant
    -- (Windows, restricted container profiles) and the live host is Windows. A refusal degrades
    -- this one field rather than losing the sample — memory is what the incident was about.
    open_sockets  INT,
    threads       INT              -- a worker/executor leak shows here before it shows in memory
);

-- The only access path: a window, ordered by time (weekly aggregate, prune by age).
CREATE INDEX IF NOT EXISTS resource_samples_ts ON resource_samples (ts);
