-- 011_article_detection_trigger — WHICH detection path raised this article's tier (ISSUE_106).
--
-- `flag_candidates` wrote `importance`, `breaking_candidate` and `flagged_at`, but never which of
-- the two paths fired:
--
--     if cluster_size >= cfg.high_cluster_size or (
--             keyword_hit and source_weight >= cfg.keyword_source_weight):
--         return HIGH
--
-- So "is the cluster path still alive?" was unanswerable from any query, report or log line after
-- the fact — only by re-deriving it from the vectors and the window. The one number the breaking
-- report offered (`flagged_candidates`) is the sum of both paths, which is why it cannot be used to
-- tune either. A threshold whose effect nobody can observe is a threshold nobody can tune.
--
-- Fixed vocabulary (types/ingest_types.py): cluster | keyword. Strict at the producing seam, plain
-- TEXT here — a row carrying a value a later version introduces must still load.
--
-- NULL = flagged before this column existed. No backfill: the decision is irreconstructable after
-- the fact (it depended on the corpus state at that instant), which is exactly why it is captured
-- at the call. Surfaces must report NULL as "unknown", never fold it into either path.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS detection_trigger TEXT;

-- The calibration question is always "how many of each path, over a window", so the index carries
-- the flag time with it: a partial index over the flagged rows only, which is a small fraction of
-- the corpus.
CREATE INDEX IF NOT EXISTS idx_articles_detection_trigger
    ON articles (detection_trigger, flagged_at)
    WHERE detection_trigger IS NOT NULL;
