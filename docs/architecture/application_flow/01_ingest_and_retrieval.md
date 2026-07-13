# Detailed Ingest Stage & Retrieval

The end-to-end path a news item takes — from the feed to the small, high-signal
article context handed to the LLM. Two paths share one corpus: the **ingest (write)
path** fills the corpus; the **retrieval (read) path** pulls a per-symbol slice out of
it. Read this before touching anything in the ingest or retrieval stage; the per-unit
detail below is the map.

Companion docs: `../pipeline_engine_architecture.md` (how the engine is wired) and
`../retrieval_policy.md` (the retrieval parameters in depth).

## Phase A — Ingest (write path)

Top-down, each new article flows through these units in order:

1. **Trigger — `core/triggers/interval_trigger.py` (`IntervalTrigger`) · built, ISSUE_10.**
   Ingest is **pull, not push**: the engine fetches on its own schedule. Nothing is
   pushed to us. (The only push path in the system is the future live breaking
   channel, ISSUE_11 — a separate concern.) The trigger loop is overlap-free (the
   next tick waits for the pass) and fires immediately on start; the **ingest worker**
   (`core/pipeline/ingest_worker.py`) clocks one **source-set**
   (`configs/source_sets/<id>.json` — feeds + ingest cadence, default 300s; declared
   once, referenced by constellations via `source_set`). Acquisition runs faster than
   eval deliberately: RSS windows slide, a missed article is gone forever — and this
   path never touches the LLM, so frequent is cheap. One worker feeds every pipeline
   referencing the set (1× fetch, N× read).

2. **Fetch — `core/sources/rss_source.py` (`RssSource.fetch`).**
   Actively pulls the RSS feed (`feedparser.parse(url)`), maps each entry to an
   `Article` (title + summary only), assigns an **idempotent** `article_id` from the
   entry guid/link, stamps `fetched_at` as real-time UTC, and carries the configured
   `source_weight` onto every article. An entry with no stable identity is skipped
   rather than allowed to poison the corpus.

3. **Embed — `core/rag/openai_embedder.py` (`OpenAIEmbedder.embed`).**
   Sends the article text to OpenAI and gets back a 1536-dimension vector — a point
   in "meaning space", where direction encodes meaning. OpenAI returns the vectors
   **L2-normalized** (unit length), which is what lets retrieval treat a dot product
   as cosine similarity later (no separate normalization step). The output width is
   pinned to the configured `dimensions`, so a config change can never desync the
   pgvector column.

4. **Store — `core/rag/pgvector_store.py` (`PgVectorStore.upsert`).**
   Writes the vector **and the full raw article** into the shared pgvector corpus,
   **idempotent** on `article_id` (`ON CONFLICT DO NOTHING`). Keeping the raw text is
   deliberate: it is what makes a later re-embed possible (e.g. an embedding-model
   change, ISSUE_16). The `importance` / `breaking_candidate` columns exist now
   (nullable) and are populated later by the breaking detector (ISSUE_11).
   **Corpus guard (built, ISSUE_16):** on first creation the store stamps the corpus
   with its embedding model + dimensions in a `corpus_meta` row; booting against a
   mismatched stamp raises hard, naming both sides — vectors from different models
   must never mix, and a config edit can never silently poison the corpus (a model
   change is a deliberate re-embed migration, ISSUE_14).

**Store everything, filter later.** Ingest never decides relevance — it embeds and
upserts *every* article. Relevance is per-query and belongs to retrieval.

**Running it.** The write path runs as one pass (`core/pipeline/ingestor.py` — `Ingestor`:
fetch → embed → upsert) with three drivers: the **ingest worker** on its source-set cadence
(`server_cli --workers`, ISSUE_10 — the live mode), the manual
`finiexragengine/cli/ingest_cli.py --source-set <id>` pass, and — only when the server runs
*without* workers — inline as `Pipeline.run`'s first stage (the self-contained manual run).
Cheap to re-run: the store is asked which article ids it already holds (`existing_ids`), so only
genuinely new items are embedded — the pass reports `embedded N` (the paid count), so a re-run
over an unchanged feed window pays nothing. Article text is embedded as `title. summary` (the
title carries signal when the RSS summary is thin).

## Phase B — Retrieval (read path)

Retrieval runs **per symbol**. Top-down, one symbol's query flows through:

1. **Symbol → query text — `core/rag/symbol_query_map.py` (`SymbolQueryMap.query_for`).**
   A raw ticker ("BTCUSD") embeds poorly, so each constellation maps it to
   retrieval-friendly text ("Bitcoin BTC"). Resolution: configured alias → derived
   base currency → the symbol itself.

2. **Resolve the query vector — `core/rag/query_vector_cache.py` (`QueryVectorCache`)
   via `core/rag/retriever.py` (`Retriever.retrieve`).**
   The retrieval queries are a fixed, small set (`symbol_queries`), so they are embedded
   **once** and cached in the `query_vectors` table (ISSUE_19); later retrievals reuse the
   stored vector instead of re-calling the API. Embedded with the **same model** as the
   articles — vectors from different models live on different maps and are not comparable
   (the invariant ISSUE_16 guards; the cache key is `(query_text, model, dimensions)`, so a
   text or model change re-embeds only what changed). This is the reference direction
   everything is compared against; because the vectors are in the DB, the ranking can also
   be reproduced by hand in SQL (see `../development/database_inspection.md`).

3. **Candidate search in the DB — `core/rag/pgvector_store.py` (`PgVectorStore.query`).**
   One SQL round-trip does three things at once: the **recency filter**
   (`published_at >= since`), the **distance ranking** (`embedding <=> query`, pgvector's
   cosine-distance operator — `0.0` = identical direction, ascending = best first), and
   the **fetch cap** (`ORDER BY distance LIMIT`). The store returns each match's stored
   embedding too, so the next step needs no re-embedding.

4. **Relevance floor — `core/rag/retriever.py` (`Retriever.retrieve`) · ISSUE_24.**
   Before dedup, candidates whose query↔article distance exceeds `floor_distance`
   (default 0.55) are dropped — nearest is not the same as *near*, and an off-topic
   article must never reach the prompt. An **empty** survivor set is a result: the
   evaluator answers it mechanically (`HOLD`, `basis='no_data'`, no LLM call).

5. **Squeeze — `core/rag/retriever.py` (`Retriever._squeeze`).**
   Walks candidates in rank order and collapses near-duplicates (the same story
   syndicated across feeds) via pairwise cosine ≥ `dedup_similarity`, then caps at
   `top_k`. **Dedup runs before the cap** so duplicates never consume a slot; each tier
   over-fetches (`_OVERFETCH`) so dedup cannot starve the cap. Result: at most `top_k`
   distinct, recent, on-topic articles.

The retrieval parameters (`top_k`, `recency_window_minutes`, `dedup_similarity`, the
optional two-tier `deep_tier`) and the ranking tie-breaks are documented in
`../retrieval_policy.md`.

## What leaves retrieval — and what does not

The comparison numbers (distance / cosine) are **ephemeral**: computed to rank, used to
select, then dropped. `Retriever.retrieve` returns `List[Article]`, not the scored
wrappers — the score does not travel into the prompt, the DB, or the envelope. What
survives is the **decision** (which articles were selected); the raw vectors stay in the
corpus, the raw text stays with them. Downstream, the selected articles become the LLM
prompt (ISSUE_6) whose structured output is persisted as the outcome envelope — that path
continues in `02_analysis_and_outcome.md`.
