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

## Configuration reference

`retrieval` block per constellation:

| Key | Default | Meaning |
|---|---|---|
| `top_k` | 12 | hard cap on articles reaching the prompt |
| `recency_window_minutes` | 1440 | recent-tier lower bound |
| `dedup_similarity` | 0.92 | pairwise cosine ≥ this collapses near-duplicates |
| `deep_tier` | absent | opt-in: `{ "min_importance": 2, "window_minutes": 43200 }` |

`symbol_queries` sits at the top level of the constellation, next to `symbols`.

## Tests

`tests/test_retriever.py` (mocked embedder/store: recency, cap, dedup, deep tier,
tie-breaks), `tests/test_symbol_query_map.py` (alias + fallback), and the paid live
round-trip in `tests/test_rag_live.py` (real embeddings → pgvector → retrieve) — see
[testing](../testing.md).
