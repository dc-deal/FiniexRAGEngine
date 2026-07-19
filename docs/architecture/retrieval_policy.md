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

## Configuration reference

`retrieval` block per constellation:

| Key | Default | Meaning |
|---|---|---|
| `top_k` | 12 | hard cap on articles reaching the prompt |
| `recency_window_minutes` | 1440 | recent-tier lower bound |
| `dedup_similarity` | 0.92 | pairwise cosine ≥ this collapses near-duplicates |
| `floor_distance` | 0.55 | query↔article distance > this drops the candidate; `null` disables; crypto constellation: 0.68 (see calibration note) (ISSUE_24) |
| `deep_tier` | absent | opt-in: `{ "min_importance": 2, "window_minutes": 43200 }` |

`symbol_queries` sits at the top level of the constellation, next to `symbols`.

## Tests

`tests/test_retriever.py` (mocked embedder/store: recency, cap, dedup, deep tier,
tie-breaks), `tests/test_symbol_query_map.py` (alias + fallback), and the paid live
round-trip in `tests/test_rag_live.py` (real embeddings → pgvector → retrieve) — see
[testing](../testing.md).
