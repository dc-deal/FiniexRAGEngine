# Retrieval Policy — the "Squeeze"

How the engine turns a per-symbol question into a small, high-signal article context
without ever sending the whole corpus to the LLM. Implemented in
`finiexragengine/core/rag/retriever.py`; parameters live in each constellation's
`retrieval` block.

## Symbol → query mapping

Retrieval runs per symbol, but a raw ticker ("BTCUSD") embeds poorly. Each constellation
maps its symbols to retrieval-friendly query text:

```json
"symbol_queries": {
    "EURUSD": "Euro US Dollar EUR/USD euro area ECB"
}
```

Resolution order (`SymbolQueryMap.query_for`):

1. the constellation alias (`symbol_queries`),
2. else the base currency derived by stripping a known quote suffix (`BTCUSD` → `BTC`),
3. else the symbol itself.

## Two-tier candidate policy (token control)

- **Recent tier (always):** the most-similar articles published inside
  `recency_window_minutes`. Recency dominates — sentiment is a current-mood signal.
- **Deep tier (opt-in per pipeline):** older articles enter only when their corpus
  `importance` tag reaches `deep_tier.min_importance`, looking back at most
  `deep_tier.window_minutes`. Meant for narrative/regime pipelines; sentiment pipelines
  stay recent-only (no `deep_tier` key = disabled).
- `top_k` is the **hard cap** on what reaches the prompt, applied after merge and dedup.
  Each tier over-fetches (2 × top_k) so dedup cannot starve the cap.
- **Background section (opt-in per pipeline, `retrieval.background`):** a second, older section for
  pipelines whose horizon is days rather than hours. Its window is
  `[now − background.window_minutes, start of the recent window)` — half-open and ending where the
  recent window begins, so no article is returned by both. It has **its own cap**
  (`background.top_k`, over-fetched 2×), is **not** importance-gated, applies the same relevance
  floor, and collapses a candidate that near-duplicates anything already kept. It is selected after
  the recent/deep squeeze and appended behind it, tier `background`, so a prompt can fence it. Why it
  is not the deep tier: the deep tier admits only what the keyword detector flagged (measured as
  almost nothing) and fills only the slots recent news leaves (rarely any) — it answers *what was
  important*, the background section answers *what has been building up*.
- **Aside section (opt-in per pipeline, `retrieval.aside`):** a class of articles set apart into its
  own labelled section. Each candidate gets a score — its similarity to `aside.anchor` minus its
  similarity to `aside.counter_anchor` (default: the pipeline's own query), both measured on the
  article's **stored** vector, so it costs no API call per pass; the two texts are embedded once and
  served from the query-vector cache. After the floor, a candidate scoring at or above `aside.margin`
  leaves the recent/deep squeeze **and** the background section, which refill from what remains
  (over-fetch rises to 3× while the section is set). The set-apart candidates are ranked like the
  windows they came from (recent before background, then distance), collapsed against everything
  already kept, capped at `aside.top_k` and appended last with tier `aside`. The use it was built for:
  a view whose prompt declares a whole class of news to be context rather than evidence. Fencing that
  class does the sorting the prompt otherwise leaves to the model — and the measured reason to do it
  is that a smaller model does not do that sorting reliably — while the main sections are freed for
  the evidence they exist to carry. A story that is half one class and half the other scores near
  zero and stays in the main section, which is what a story carrying the main class's event should do.

## Corpus scope — `declared` source sets

Retrieval is **corpus-wide** by default: a pipeline reads every set's articles, and its `source_set`
decides only ingest, breaking wakes and source reach. The consequence is easy to miss — a feed added
to *any* set enters the evidence of *every* pipeline without moving their fingerprints. A set with
`"scope": "declared"` is read only by the pipelines whose `source_set` it is: every other pipeline's
retriever excludes its active sources in the store's scan itself (`AND NOT (source_id = ANY(...))`),
in every window, so an excluded article can never take a candidate slot. A source also active in a
shared set stays visible — that set publishes it to everyone by its own terms. One computation
(`SourceSetRegistry.excluded_source_ids`) serves the pipelines and the corpus reports (`coverage`,
`floor_profile`), so a report measures the corpus the pipeline actually reads. At boot each declared
set is announced with how many pipelines may read it — a declared set no pipeline is on is warned
about, since it is ingested by none and read by none:

```
[SCOPE] <set> · declared · 5 active feeds · readable by 1 pipeline(s), excluded from every other
```

What it is for: a new line of work can widen its own corpus without reaching into anyone else's
evidence — in particular not into a series inside a measurement's held-out window.

## Ordering

Candidates rank by: tier (recent before deep) → ascending cosine distance → higher
`source_weight` → higher `importance`. Distance is rounded to 4 decimals before
comparison so float noise does not defeat the tie-breaks.

## Near-duplicate collapse

The same story syndicates across feeds. Candidates are walked in rank order; a candidate
whose stored embedding has a pairwise cosine similarity ≥ `dedup_similarity` (default
0.92) with an already-kept article is dropped. The store returns the stored embeddings
with each match, so dedup needs no re-embedding and no LLM.

## Relevance floor (ISSUE_24)

Nearest is not the same as *near*: for a symbol with no news of its own, the top-k
"nearest" candidates are still generic, off-topic articles. Before dedup, any candidate
whose query↔article cosine **distance** (`embedding <=> query`, = 1 − similarity) exceeds
`floor_distance` is dropped. Note the axis: `dedup_similarity` cuts what is **too
similar** to another article (redundancy); the floor cuts what is **too dissimilar** to
the query (irrelevance).

**Calibration is query-length dependent** (coverage report, 2026-07-19): short symbol
queries ("Bitcoin BTC") embed systematically further from article texts — on-topic lands
~0.60–0.66, generic crypto ~0.70+, so the crypto constellation uses **0.68**. Long,
specific queries (forex: "Euro US Dollar EUR/USD euro area ECB") land ~0.37–0.46, so
**0.55** (the schema default) holds there. Tune with the coverage report's what-if flag
(`coverage_cli --floor X`); the `n≤f` column predicts the live context per symbol.

An **empty** survivor set is a legitimate result: the evaluator answers it with the
mechanical contract row (`HOLD / 0.0 / 'No relevant news found' / []`, tagged
`basis='no_data'`) **without an LLM call** — no tokens, no cost, logged as `[NO_CONTEXT]`
for traceability. The coverage report's `n≤f` column predicts exactly this: how many
window articles survive the floor per symbol (0 → the mechanical HOLD).

## Funnel counters

Every retrieval records how it arrived at its context (`RetrievalFunnel`): candidates
**in window**, **floor-dropped**, **tier-** and **near-duplicates** collapsed, **kept**,
plus the pre-floor distance spread (`best_distance`/`worst_distance` — best doubles as
the "nearest miss" when everything was dropped) and the **`floor` applied on this run**
(snapshot, so a persisted envelope stays interpretable after a retune). The `eval` CLI
renders the spread with the floor's position as % of the span
(`min 0.601  [27%]  floor 0.68  [73%]  max 0.892`) — the live calibration view: 0% below
the floor means nothing passes. The funnel travels
with the evaluation (`SymbolEval.retrieval`) into the envelope
(`metadata.per_symbol_retrieval`, additive/non-load-bearing) and renders as the
`retrieval` line in the `eval` CLI — so a thin or empty context is explainable from the
persisted run, not just asserted: was the window empty, or did the floor cut everything?

**Which tier surfaced each kept article travels too** (ISSUE_30). `Retriever._squeeze` had the
tier in its own loop and returned bare `Article`s, so nothing downstream could tell a week-old
retrospective item from an hour-old one except by reading its timestamp — and the v4 templates
re-sort by `published_at`, mixing both into one numbered list. It now returns
`RetrievedArticle(article, retrieval_tier)`; `RetrievedContext.articles` stays available as a
**property** so a shipped prompt renders byte-identically, and `RetrievalFunnel.deep_kept` is
derived from that list rather than accumulated beside it — one computation, so the funnel's count
and the envelope's per-citation tiers cannot disagree. The background section is counted on its
own — `background_in_window`, `background_floor_dropped`, `background_kept` — so every counter above
keeps exactly the meaning it had in the archive, and `background_kept` is not part of `kept`. The
aside section adds `aside_moved` (candidates the anchor set apart, every window, after the floor) and
`aside_kept` (what reached the prompt under its own cap), part of neither `kept` nor
`background_kept`; both are 0 for a pipeline without it. The tier is a closed vocabulary
(`RetrievalTier`), named in full because *tier* already means the **importance** tier (1/2/3)
elsewhere; the retriever's rank key maps it back to an explicit order, since sorting the labels
would put `deep` ahead of `recent` alphabetically and rank week-old articles above today's news.

The weekly report aggregates these persisted funnels into the **no-data / coverage
block** (ISSUE_27): per-symbol share of `no_data` passes, nearest miss vs the floor
snapshot, and a *calibration-candidate* flag when a mostly-silent symbol's nearest miss
sits within 0.02 of the floor — the operator's retune signal until ISSUE_55 automates it.
See `docs/architecture/weekly_report_and_alerts.md`.

## Configuration reference

`retrieval` block per constellation:

| Key | Default | Meaning |
|---|---|---|
| `top_k` | 12 | hard cap on articles reaching the prompt |
| `recency_window_minutes` | 1440 | recent-tier lower bound |
| `dedup_similarity` | 0.92 | pairwise cosine ≥ this collapses near-duplicates |
| `floor_distance` | 0.55 | query↔article distance > this drops the candidate; `null` disables; crypto constellation: 0.68 (see calibration note) (ISSUE_24) |
| `deep_tier` | absent | opt-in: `{ "min_importance": 2, "window_minutes": 43200 }` |
| `background` | absent | opt-in: `{ "window_minutes": 20160, "top_k": 8 }` — its window must exceed `recency_window_minutes` (refused at load otherwise); absent is omitted from `config_fingerprint`, so adding the key to the schema forked no existing series |
| `aside` | absent | opt-in: `{ "anchor": "…", "counter_anchor": "…", "margin": 0.05, "top_k": 2 }` — `counter_anchor` defaults to the pipeline's query; an empty anchor, `top_k < 1` or a margin outside [−1, 1] is refused at load; absent is omitted from `config_fingerprint` |

On the source set: `scope` — absent (shared with every pipeline) or `"declared"` (read only by the
pipelines on the set); absent is omitted from the source-set half of `config_fingerprint`.

`symbol_queries` sits at the top level of the constellation, next to `symbols`.

## Tests

`tests/rag/test_retriever.py` (mocked embedder/store: recency, cap, dedup, deep tier,
background section — window bounds, own cap, cross-section dedup, its counters — the aside section —
anchor split, main and background refill, mixed items staying, the margin, the counter defaulting to
the query, cap and dedup inside the section, overfetch — the exclusion reaching every window —
tie-breaks), `tests/configuration/test_source_set_registry.py` (which sources each set excludes),
`tests/rag/test_symbol_query_map.py` (alias + fallback), and the paid live
round-trip in `tests/rag/test_rag_live.py` (real embeddings → pgvector → retrieve) — see
[testing](../testing.md).
