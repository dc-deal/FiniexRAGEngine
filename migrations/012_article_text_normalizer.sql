-- 012_article_text_normalizer — the fetched bytes, and which treatment produced the stored text
-- (ISSUE_112).
--
-- Article text reached the embedder, the eval prompt and the breaking keyword match exactly as the
-- feed served it. Measured over 1,966 dev articles: 50.5 % carry HTML markup, 21.2 % carry entities,
-- and 36.7 % of every token the engine pays for is markup. The same markup produced the first
-- measured false-positive class in the one detection gate that needs no corroboration — 6 of 99
-- keyword hits were a CDN's stock-image filenames on a weight-1.0 source, each enough on its own to
-- flag an article HIGH and wake the eval.
--
--   title_raw / summary_raw   the text as fetched, written ONLY where normalisation changed it
--   text_normalizer           the declared profile that produced the stored text and the vector
--
-- The raw pair is what keeps the ingest rule intact: "store the full raw corpus, never discard at
-- ingest" — markup is removed from what the model reads, not from what the engine holds. NULL means
-- "arrived clean", not "not measured", which is why it costs ~633 B on roughly half the corpus
-- instead of doubling it. An injection investigation then gets the exact bytes rather than a URL
-- whose feed has rolled over.
--
-- `text_normalizer` follows the ISSUE_79 pattern (`embed_input_tokens`): the row records what
-- produced it instead of leaving it to be inferred from when it was stored. Fixed vocabulary at the
-- producing seam (types/ingest_types.py: v1), plain TEXT here — a row carrying a profile a later
-- version introduces must still load.
--
-- Additive and nullable, and deliberately NOT backfilled. The change is forward-only: the corpus
-- upserts ON CONFLICT (article_id) DO NOTHING and the ingestor skips ids it already holds, so
-- existing rows keep their text and their vectors. Re-embedding is paid work that buys nothing the
-- stamp does not already record. The mixing is bounded rather than open-ended —
-- `recency_window_minutes` is 1440/2880, so ordinary retrieval is entirely normalised within two
-- days; the named tail is the deep tier (importance >= 2), which can reach older rows.
--
-- NULL on `text_normalizer` therefore reads as "stored before the treatment existed" and must never
-- be folded into a profile by a surface that renders it.

ALTER TABLE articles ADD COLUMN IF NOT EXISTS title_raw       TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS summary_raw     TEXT;
ALTER TABLE articles ADD COLUMN IF NOT EXISTS text_normalizer TEXT;

-- No index. Every question these columns answer is either a point lookup by `article_id` (the
-- forensic case) or a full-corpus census run once per investigation — neither is a read pattern an
-- index would serve, and the corpus takes an insert on every ingest pass.
