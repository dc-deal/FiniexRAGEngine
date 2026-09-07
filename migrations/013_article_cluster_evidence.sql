-- 013_article_cluster_evidence — the neighbourhood a cluster flag was made on (ISSUE_106).
--
-- The detector computes a cluster size to decide a tier and then discards it. So "how many flags did
-- the cluster path make" was answerable from `detection_trigger` (migration 011), while "were those
-- flags justified" was not answerable at all after the fact — it needed replaying the corpus through
-- `detection_sweep`, which says what a setting *would* do rather than what the running one *did*.
--
--   cluster_articles   near-duplicate articles in the window, within the similarity gate
--   cluster_feeds      how many DISTINCT feeds those articles came from
--
-- Both, because the **gap between them is the intra-feed duplication** and neither number carries it
-- alone. A story on three outlets reads 3 and 3; one feed's live-blog reaching a cluster of three
-- reads 3 and 1. Measured 2026-09-07 over 2,000 seeds, the article count admits 12 neighbourhoods at
-- similarity 0.75 where the feed count admits 7 — the difference is entirely one feed corroborating
-- itself, which is what made the cluster path unusable while it counted rows.
--
-- Written ONLY where the cluster path produced the verdict. A keyword flag leaves both NULL rather
-- than writing 0, because 0 would claim an empty neighbourhood was measured when none was consulted
-- — the same distinction `detection_trigger` draws between a category and an absence, and the reason
-- both columns are set in their own clause instead of always.
--
-- Additive, nullable, up-only, not backfilled: the counts describe a decision taken at a moment with
-- a corpus that has since changed, so a reconstruction would be a different measurement wearing the
-- same column name.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS cluster_articles SMALLINT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS cluster_feeds    SMALLINT;

-- No index. The read pattern is a windowed scan by `flagged_at` for a report run occasionally, which
-- an index on either count would not serve — and the corpus takes an insert on every ingest pass.
