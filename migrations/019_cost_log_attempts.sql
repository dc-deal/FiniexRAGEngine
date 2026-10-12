-- 019_cost_log_attempts — one row per attempt, not per successful call (ISSUE_128).
--
-- The OpenAI SDK retried every call on its own (2 retries by default) and only the attempt that
-- answered reached the billing log: its `duration_ms` held the hidden attempts and the backoff,
-- and an attempt that timed out — possibly billed — left no row at all. The engine now runs the
-- retry loop itself and records every attempt, so a row says which try it was and how it ended.
--
-- `outcome` is a fixed vocabulary (types/llm_types.py): ok | timeout | connection | rate_limited |
-- server_error | rejected | quota. TEXT, not an enum: a value a later version adds must still load.
--
-- History: `outcome` defaults to 'ok', which is true — only successes were ever written. `attempt`
-- stays NULL, because whether a hidden SDK retry sat inside an old row is no longer knowable; a
-- plausible 1 would be exactly the papering-over default the output contract forbids.
--
-- A failed attempt has no usage block, so its tokens and USD are NULL — "unknown whether billed",
-- never a plausible 0. SUM() skips NULL, so every existing total keeps its meaning.

ALTER TABLE cost_log ADD COLUMN IF NOT EXISTS attempt SMALLINT;
ALTER TABLE cost_log ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE cost_log
    ALTER COLUMN prompt_tokens DROP NOT NULL,
    ALTER COLUMN completion_tokens DROP NOT NULL,
    ALTER COLUMN total_tokens DROP NOT NULL,
    ALTER COLUMN usd_cost DROP NOT NULL;
