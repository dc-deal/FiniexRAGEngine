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

   **The catalogue vs. what runs.** A set's `sources` is the *declared catalogue*;
   `SourceSetConfig.active_sources()` is what the engine actually builds and polls — the
   single definition read both by the ingestor and by `SourceReach`, which takes the
   envelope's reach census over it, so the set that runs and the set that is reported cannot
   drift apart. Two per-source fields drive it:

   - **`enabled: false`** — declared but switched off: never built, never polled, no health
     event, and invisible downstream (in neither envelope reach number, nor in `errors`) —
     switching a feed off is a decision, not a degradation, so it must not be reported as a
     source the run failed to reach. Same idiom as a disabled model variant (ISSUE_42).
   - **`comment`** — editorial knowledge about the feed. JSON has no comments, so this is
     the sanctioned place to record what was learned ("high-trust FX source"; "behind
     Cloudflare from datacenter IPs").

   **Where to switch a feed off matters.** Reachability is often an *environment* fact, not
   a property of the feed: a source behind bot-management answers a clean egress IP with
   `200` and a datacenter IP with `403` (a JS challenge no feed reader can pass). Deleting
   it from the tracked set would throw away the knowledge and lie about the world. So the
   tracked `configs/source_sets/` stays the canonical catalogue, and the *machine-specific*
   switch lives in the gitignored **`user_configs/source_sets/<id>.json`**, deep-merged at
   load. Because `sources` merges **by `source_id`**, the override names only the feed it
   changes:

   ```jsonc
   // user_configs/source_sets/forex_news.json — this machine only
   { "source_set_id": "forex_news",
     "sources": [ { "source_id": "fxstreet", "enabled": false,
                    "comment": "Cf-Mitigated: challenge from this egress IP." } ] }
   ```

   The other feeds are inherited untouched. A switched-off feed is **marked, never hidden**, on
   every operator-facing surface: `feed_doctor` deliberately still probes it (`OK [disabled]`) —
   it is the tool that answers "can I turn this back on yet?" — and the Sources health report
   appends the same `[disabled]` marker to its verdict. The marker is appended rather than
   substituted, because the health record is *how the feed behaved while it was polled*, which is
   exactly what the decision to re-enable rests on. Note what this costs: `enabled` is a config
   fact and `source_health` has no column for it, so the report has to be *told* by its CLI.
   Without the marker a disabled feed's frozen last poll reads `ok` forever — stale history
   dressed as a live verdict. (Downstream is the exception: the envelope never sees a disabled
   source at all. Operators get the truth; consumers get the contract.)

   **Every source is accounted for — the pass reports all of them.** A pass records exactly one
   `SourcePoll` per source it considers (`ok`, `failed`, `quarantined`, `floor_skipped`,
   `suspended`), appended in config order; `IngestResult.polls` is the single record and the
   dict views (`per_source`, `failed_sources`, `quarantined_skips`, `floor_skips`) are derived
   from it. This matters because it was once otherwise: each fate went into its own collection,
   the CLI iterated two of them, and a feed in **quarantine therefore vanished from the output
   entirely** — a permanent HTTP 403 rendered as a clean run. `reports/ingest_report.py` renders
   against the *declared catalogue*, so a switched-off feed and one the pass never reached
   (a mid-pass budget suspend) also get a labelled line instead of silence:

   ```
   sources: 8 declared · 6 polled · 1 quarantined · 1 disabled
   fxstreet         disabled            —         —     —     —  Disabled on this machine …
   forexlive        ok                 25         0     0    25
   boe_news         QUARANTINED         —         —     —     —  3h left
   ```

   The `IngestWorker` cannot use the same table (a quarantine outlives thousands of passes on a
   15s cadence), so it carries the skip count on the pass line it logs anyway — and in the
   `WorkerState` the API serves — while the per-skip line stays DEBUG. Entering quarantine still
   WARNs once.

   **Reach — the envelope's two source numbers.** `core/observability/source_reach.py`
   (`SourceReach.census`) is the one place a set's config and its feed health are combined, and
   the only source of `metadata.sources_configured` / `sources_reached`:

   - `configured` = `len(active_sources())` — a **disabled** feed is in neither number. Switching
     a feed off is a *decision, not a degradation*; reporting it as unreached would claim a
     contribution that never existed.
   - `reached` = of those, the ones `source_health` says are delivering (not in cool-off, last
     poll succeeded). Read **live** per run, never from the store's in-memory quarantine cache:
     the reader is usually a different instance from the writer.

   This replaced `sources_configured - len(failed_sources)`, which derived one number from the
   other and so could only ever differ by a *failed fetch*. Everything else — a quarantined feed,
   an aborted pass — counted as reached; and in **worker mode the runner has no pass at all**
   (acquisition is the ingest worker's clock), so the field was `configured` on every single run:
   a full reach the run never attempted, in the one mode that ships. Reading health works in both
   modes because acquisition records every poll there regardless of who ran it (CLAUDE.md —
   *capture at the call, report from the store*).

   A source within its **poll floor** needs no special case: a floor skip deliberately records no
   health, so the feed keeps its last real verdict — correct, since its articles are in the corpus
   either way. And reach is **telemetry, not control**: `_derive_status` reads `errors`, never
   these counts, so a gap is reported without silently reclassifying a run.

2. **Fetch — `core/sources/rss_source.py` (`RssSource.fetch`).**
   Actively pulls the RSS feed, maps each entry to an `Article` (title + summary only),
   assigns an **idempotent** `article_id` from the entry guid/link, stamps `fetched_at`
   as real-time UTC, and carries the configured `source_weight` onto every article. An
   entry with no stable identity is skipped rather than allowed to poison the corpus.
   **Conditional GET (ISSUE_11):** the long-lived source keeps each feed's `ETag` /
   `Last-Modified` and sends them back, so an unchanged feed answers `304` with no body —
   this is what lets the ingest clock run near-continuous (~15s, for flash-crash latency)
   while staying polite; the binding constraint at speed is feed etiquette, not OpenAI.
   An optional per-source `poll_interval_seconds` lets a slow feed opt out of the fast
   loop (central-bank feeds are deliberately *not* slowed — they are prime breaking
   sources; 304 keeps them fast and polite). **Status-aware + health-tracked (ISSUE_11):**
   the fetch classifies every outcome into a typed `SourceFetchError`
   (`RATE_LIMITED` on HTTP 429, `HTTP_ERROR`, `UNREACHABLE` with one retry, `PARSE_ERROR`)
   instead of parsing a non-feed error body, and every poll — success or failure — is
   recorded into `source_health`; a feed that keeps failing is flagged and quarantined so
   the loop backs off. See [`source_health_and_logging.md`](../source_health_and_logging.md).

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
   change, ISSUE_16). The `importance` / `breaking_candidate` / `flagged_at` columns are
   populated by the breaking detector in step 5 (ISSUE_11).
   **Corpus guard (built, ISSUE_16):** on first creation the store stamps the corpus
   with its embedding model + dimensions in a `corpus_meta` row; booting against a
   mismatched stamp raises hard, naming both sides — vectors from different models
   must never mix, and a config edit can never silently poison the corpus (a model
   change is a deliberate re-embed migration, ISSUE_14).

5. **Breaking detection — `core/pipeline/breaking_detector.py` (`BreakingDetector`) · built, ISSUE_11.**
   After upsert, an **LLM-free** pass flags breaking candidates over the articles just stored:
   cluster-burst (near-duplicate count via `count_neighbors`) + a keyword fast-path on high-trust
   sources → writes an `importance` tier + `breaking_candidate` + `flagged_at` onto the corpus rows
   (`flag_candidates`). The highest tier drives the eval **wake** (the `BreakingBus`), so a flash
   crash is evaluated in seconds instead of up to a full eval interval. Full detail — the two-
   parameter split, the reaction-time anchors, continuous-ingest etiquette — in
   `../breaking_detection.md`.

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
