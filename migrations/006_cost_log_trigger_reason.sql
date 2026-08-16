-- 006_cost_log_trigger_reason — why the pass that made this call ran (ISSUE_87).
--
-- A cost row said what was spent and by which pipeline, never what set the pass in motion. So
-- "what do out-of-band breaking wakes actually cost us" — a question #69's monthly statement will
-- ask directly — was not answerable from the billing log at all, and "is this call from the boot
-- pass or from the bar-close tick" was not answerable from anywhere.
--
-- Fixed vocabulary (types/trigger_types.py): scheduled | boot | breaking | manual | external.
-- Written from the pass scope, so one binding covers every paid call the pass makes — the LLM
-- eval, the query embeddings, and the ingest embeddings that have no envelope to carry the fact.
--
-- NULL = recorded outside a pass scope, or before this column existed. No backfill: the reason is
-- irreconstructable after the fact, which is the whole point of capturing it at the call.

ALTER TABLE cost_log ADD COLUMN IF NOT EXISTS trigger_reason TEXT;
