-- 003_article_embed_tokens — what the embedding actually saw, per article (ISSUE_79).
--
-- An article whose text exceeded the embedding model's 8192-token input limit made the whole
-- batch return HTTP 400, which failed the entire ingest pass — and because nothing was stored,
-- the offending item stayed "new" and came back every pass until its feed dropped it. For ~30
-- hours every article batched with it was silently never ingested, while source_health reported
-- the feed healthy the whole time (the failure is one stage *after* the poll it records).
--
-- Over-long inputs are now trimmed to fit instead of rejected, and these two columns keep that
-- honest. The embedded string is `title. summary`, derived per pass and never stored — so the
-- columns above remain the untouched original, and these describe only the embedding input:
--
--   embed_input_tokens      what was actually sent
--   embed_truncated_tokens  how many tokens were cut (NULL = nothing was cut)
--
-- Their sum is the original length, so the pair answers every question without a third column,
-- and the untouched title/summary make the trim reversible for later study (re-embed the full
-- text and judge how far the cut moved the signal).
--
-- Recomputable from title/summary, and stored anyway: it turns per-source analysis into a plain
-- aggregate instead of a Python pass that re-encodes the corpus, and it records what happened
-- rather than what today's tokenizer would say — the same reason cost_log.usd_cost is frozen at
-- record time instead of re-derived from the price table.
--
-- Additive and nullable: every pre-existing row keeps NULL, which reads correctly as "we did not
-- measure this one" rather than "nothing was trimmed".

ALTER TABLE articles ADD COLUMN IF NOT EXISTS embed_input_tokens     INTEGER;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS embed_truncated_tokens INTEGER;
