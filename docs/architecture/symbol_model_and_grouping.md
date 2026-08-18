# Symbol model & query grouping (ISSUE_70)

How a pipeline declares its instruments, how the pair legs reach the output, and how symbols that
share a retrieval query collapse into **one** LLM analysis fanned out to each label.

Companion: `live_display.md` (how the grouping renders), `breaking_detection.md` (the episode
collapse), `application_flow/02_analysis_and_outcome.md` (the eval flow this sits in).

## The config shape — `SymbolSpec`

A pipeline's `symbols` is a list of objects, not bare tickers
(`types/config_types/pipeline_config_types.py`):

```json
"symbols": [
  { "key": "ETHUSD", "base": "ETH", "quote": "USD", "query": "Ethereum ETH" },
  { "key": "ETHEUR", "base": "ETH", "quote": "EUR", "query": "Ethereum ETH" }
]
```

- **`key`** — the ticker (the output `symbol`, the consumer's join key).
- **`base` / `quote`** — the pair legs, emitted as `base_currency` / `quote_currency` on every
  `SentimentResult` (additive, non-load-bearing → no `schema_version` bump). A consumer reads the
  split without its own lookup. Validated at load: **`key == base + quote`** or a config error
  (fail-fast, like the timeframe/model validators).
- **`query`** — the readable asset name the prompt + retrieval use (the template's `{{ symbol }}`
  slot receives this, *not* the ticker); engine-internal, never emitted. Falls back to `key`.
- **`enabled`** (default `true`) — `false` switches the symbol off for the whole pipeline. Symbols
  merge **by `key`** in a user override (`_OVERRIDE_LIST_KEYS`), so one line toggles one symbol
  without restating the list: `{ "symbols": [ { "key": "DASHUSD", "enabled": false } ] }`.

## Query grouping — one analysis, N labels

Symbols that differ only in quote currency are the **same analysis** for news sentiment: `ETHUSD`
and `ETHEUR` share the query `"Ethereum ETH"`, so retrieval (a cached query vector) and the prompt
are identical — the quote is irrelevant to the news mood. The runner groups active symbols by their
retrieval query, **evaluates once per unique query**, and **fans** the result out to each symbol
label (`pipeline_runner._group_by_query` / `_fan`):

```
for query, specs in group_by_query(active_symbols):
    ev = evaluate(canonical, query)              # ONE retrieve + LLM call for the group
    for spec in specs:                           # fan out: one relabelled copy per symbol,
        results.append(fan(ev.result, spec))     # its own symbol + base/quote, same analysis
```

- **Cost:** one LLM call per *unique query*, not per symbol — `crypto_sentiment` runs 8 calls for 9
  symbols (ETHUSD/ETHEUR share one). Tokens/retrieval are billed to the **canonical** (the first
  symbol of the group); the fanned labels carry no extra tokens.
- **Consistency (the real win):** the fanned rows are copies — identical `signal` / `sentiment_score`
  / `urgency` / `is_breaking` / `reasoning` / `sources`. Two independent calls on the same input
  diverged ~17% of the time on the breaking flag alone (a stochastic threshold on `urgency`); one
  analysis makes the labels agree **by construction**.
- **Contract preserved:** every requested symbol is still a row in `result` (#9) — grouping is
  invisible on the wire, the rows are just consistent now.
- **Forex is self-safe:** each pair has a distinct query (`EURUSD` = "Euro US Dollar EUR/USD…",
  `EURGBP` = "Euro British Pound EUR/GBP…"), so pairs are **never** grouped — no special rule, the
  query *is* the grouping key. (Grouping same-base FX pairs would be wrong: the quote is load-bearing
  there.)

## Breaking episodes collapse to the analysis

Because the fanned rows are both `is_breaking`, keying breaking episodes on the *ticker* would
double-count one story (ETHUSD + ETHEUR = two episodes for one ETH event). So the episode is keyed
on the **retrieval query** — in the live `BreakingEpisodeTracker` and the store reports alike —
collapsing fanned same-query symbols into **one** episode. Falls back to `base_currency`, then the
ticker, for a symbol no longer in the config (a retired or renamed stream still sitting in the
archive).

> **This key was `base_currency` until ISSUE_82.** That is the same mistake the signal chips made
> and this document already warns about two sections up: it grouped `USDJPY`, `USDCAD` and `USDCHF`
> into one episode because they share the base `USD`, when they are three separate analyses. It
> fired on 2026-08-18 — only USDCAD broke, but every pass of all three fed the `USD` key, and
> USDJPY (in the hold band ~49 % of the time) kept re-anchoring the episode gap on a story it had
> no part in. A USDCAD episode could then only close once *USDJPY* went quiet.
>
> The correction is the rule this document states at the end: **the query is the operational key;
> `base`/`quote` are the output labels, not the grouping trigger.** One derivation now serves every
> surface — `EpisodeGrouping.key_for` in `core/pipeline/breaking_episode_rule.py`.

## Verifying the grouping (from a live run)

The persisted envelope carries the proof — `/latest` or the outcome store:

- `metadata.per_symbol_tokens` lists the **canonical** (`ETHUSD`) but **not** `ETHEUR` — billed once.
- `ETHUSD.reasoning` and `ETHEUR.reasoning` are **byte-identical** (two separate calls would differ
  in wording most of the time).

The live display shows it as a merged chip (`ETH·USD/EUR:HOLD`) and a `N sym / M calls` count; the
call count is *not* visible in the pass-line "N llm" (that counts result rows).

## Not in scope

The prompt template variable rename (`{{ symbol }}` → `{{ query }}`) rides #64 Phase 2's `v3` bump.
The `signal_capability {base}` rule discussed early was **superseded** by query grouping — the query
is the operational key; `base`/`quote` are the output labels, not the grouping trigger.
