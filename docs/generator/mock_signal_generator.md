# Mock Signal Generator

`experiments/mock_signal_data/generate.py` emits JSONL that mimics the archive of
FiniexRAGEngine sentiment envelopes, so **FiniexTestingIDE** can be built and tested before the
real collector (#9) exists.

**Not for correctness.** Plausible, schema-valid mock data only — no market truth, no analytical
value. Its job is to exercise a consumer's code paths.

Two documents:

- **this one** — what it produces, how to run it, and the contracts it must not break.
- [Calibration](mock_calibration.md) — the measured real-world numbers behind every constant, and
  the known remaining divergences. Read that one before changing a distribution.

---

## The one rule

**Generated output must be distinguishable from engine output at every level.** The IDE once
imported a generated week and a real week and found *byte-identical* provenance — same
`pipeline_id`, same `prompt_id`/`prompt_version`/`prompt_hash`. Only the date told them apart, an
unwritten rule no tool checks.

Three independent markers now carry that fact, deliberately redundant:

| Marker | Value | Why it alone is not enough |
|---|---|---|
| `data_origin` | `"synthetic"` (engine: `"live"`) | a field can be dropped by a re-serialising importer |
| `pipeline_id` | `*_mock` suffix | a naming convention a later import can bypass |
| `prompt_hash` | `mock-` prefix | survives truncation to 8 chars, where the raw hash did not |

The `mock-` prefix matters because a 12-character hash is displayed truncated almost everywhere.
`f6e09cf6` alone read identical for the real forex series and its mock — two sources, one real and
one invented, indistinguishable in every visible field:

```
📡 Source: forex_macro_sentiment        (prompt sentiment-forex v1 · #f6e09cf6)
📡 Source: forex_macro_sentiment_mock   (prompt sentiment-forex v1 · #f6e09cf6)   ← same
```

`prompt_id` and `prompt_version` stay **unchanged** (`sentiment-crypto` / `sentiment-forex`): the
prompt really *is* the same one, and the dataset should say so. One field per fact.

---

## Running it

> **The symbol lists below are load-bearing — do not shorten them to save time.** `--symbols` sets
> how many random draws each cycle consumes, so changing the *count* produces a different
> realization: different signals, different scores, different pass timings, a different envelope
> count per day. It is not a subset of the longer run, and a consumer holding an earlier week cannot
> merge the two. They mirror the symbol sets of `configs/pipelines/*.json`; check them against those
> before regenerating. (2026-08-25: a regeneration went out with 8 crypto and 2 forex symbols
> because these commands had drifted from what was actually shipped. The consumer could not import
> it.)

> **Why a regeneration is nonetheless cheap today, and what would end that.** The consumer absorbed
> the 2026-08-25 realization change with **zero test failures across 2,347 passes** (one deliberate
> skip), on days where every single row had changed. That was not luck: their consumers of those days
> assert *invariants*
> rather than values — a partition identity, a directional threshold their cadence always crosses, a
> carved-versus-clean comparison. The one case that could have flipped had 11 minutes of margin
> against our largest inter-envelope gap, which moved 16.7 → 19.0 minutes.
>
> So the property to preserve is theirs, not ours: **the day one of those assertions pins a sentiment
> value, a regeneration stops being free.** Worth knowing before planning one — and it is why the
> ISSUE_108 archive re-export is expected to be cheap on their side too.
>
> **And there is now a signal for when it has stopped being true.** The consumer has written their
> own rule — tests reading our fixture pin invariants, not values — and told us how to read a breach
> of it: *"treat a request from us to keep a realization as a signal that we broke our own rule."*
> So a request to preserve a realization is **diagnostic information, not just a request**. Answer it,
> and ask which assertion started depending on a value; that is cheaper to fix on their side than a
> preserved realization is on ours.


```bash
# 5-cycle fixture sample → tests/fixtures/signals/ (tracked; the IDE's contract sample)
python experiments/mock_signal_data/generate.py

# the crypto week
python experiments/mock_signal_data/generate.py \
    --pipeline-id crypto_sentiment_mock --prompt crypto \
    --start 2026-04-27T00:00:00Z --cycles 1008 --rotate daily \
    --symbols BTCUSD,ETHUSD,ETHEUR,SOLUSD,ADAUSD,XRPUSD,DASHUSD,LTCUSD,DOTUSD \
    --out data

# the forex week
python experiments/mock_signal_data/generate.py \
    --pipeline-id forex_macro_sentiment_mock --prompt forex \
    --start 2026-04-27T00:00:00Z --cycles 1008 --rotate daily \
    --symbols EURUSD,GBPUSD,USDJPY,AUDUSD,EURGBP,NZDUSD,USDCAD,USDCHF \
    --out data
```

Both weeks also exist as a `launch.json` entry (**🧪 Mock Data**, in the `06_output` group next to
the real exporter — deliberately adjacent, so the synthetic/real distinction is made where someone
looks for it).

### Flags

| Flag | Default | Notes |
|---|---|---|
| `--cycles` | 5 | number of **bars**, not envelopes — see mechanism B below. 1008 = 7 days at M10 |
| `--start` | `2026-04-27T00:00:00Z` | ISO8601 UTC |
| `--seed` | 42 | deterministic output |
| `--symbols` | the 8 backtestable crypto pairs | **Pass the full pipeline symbol set for anything delivered** — the count drives the rng draws, so a shortened list is a *different realization*, not a smaller one. The default's older rationale (`DOTUSD` has no tick data on the consumer side) states a true fact and draws the wrong conclusion from it: the consumer's signal symbols are a superset of their price symbols **by design**, so a symbol they cannot backtest is still one they run live, import and assert against. Settled with them 2026-08-25. |
| `--pipeline-id` | `crypto_sentiment_mock` | **keep the `_mock` suffix** when overriding |
| `--prompt` | `crypto` | `crypto` \| `forex` — which real prompt this batch mocks |
| `--variants` | — | fan-out (#42): `"mini=gpt-4o-mini,4o_enhanced=gpt-4o"` |
| `--rotate` | — | `daily` \| `weekly` — the collector's bucketed layout (#13) |
| `--out` | `data` when `--rotate` is set, else the tracked fixture path | a file, or a directory root with `--rotate`/`--variants`. **A rotated run defaults to the repo's `data/`**, so the handover shape is what you get without passing a path — see the layout section. |

**`--prompt` is explicit on purpose — never inferred from `--pipeline-id`.** That id is free text;
a substring guess would be a silent wrong answer for any id that does not match the guess, and
silently wrong is the worst kind. Omitting it on a forex batch stamps `sentiment-crypto`, i.e.
provenance the data does not have.

---

## Output

### Line format

One JSONL line per envelope: the full `AnalysisEnvelope` (typed by
`finiexragengine.types.outcome_types`, so a schema change here cannot silently drift), one
`SentimentResult` per symbol, plus a top-level **`collected_msc`** — epoch milliseconds, UTC, the
IDE's merge key (nearest snapshot with `collected_msc <= tick.collected_msc`; no look-ahead).

### File layout with `--rotate daily`

```
<out>/<stream_id>/<bucket>.jsonl
```

**A rotated run writes into `data/` by default**, so the stream directory sits at the root and the
tree is the handover shape — the two pipeline ids side by side, nothing wrapping them:

```
data/crypto_sentiment_mock/2026-04-27.jsonl … 2026-05-03.jsonl
data/forex_macro_sentiment_mock/2026-04-27.jsonl … 2026-05-03.jsonl
```

Anything **not** in this layout — a single-file run, a variant fan-out — stays under
`data/mock_signals/` beside them, so the root holds streams and only streams. `data/signal_export/`
is the real exporter's tree and is never a `--out` target: keep the `_mock` suffix on
`--pipeline-id` and a generated stream can never be mistaken for a produced one.

Buckets are named from each line's `collected_msc` via `finiexragengine.utils.archive_layout` —
the same naming contract the real exporter uses (#13), so a consumer's multi-file range read can be
smoke-tested against it: `daily` → `2026-04-27`, `weekly` → ISO `2026-W18`. Combines with
`--variants` (one bucket set per stream). Full contract:
[output_archive_layout.md](../architecture/output_archive_layout.md).

### Variant fan-out (#42)

`--variants "sub_id=model,…"` renders one constellation through N mock models as **separate
streams** — format A, as confirmed with the IDE on 2026-07-11:

- The **first** entry is the default variant and keeps the bare `pipeline_id`
  (`crypto_sentiment_mock`); the others get `<pipeline_id>_<sub_id>`
  (`crypto_sentiment_mock_4o_enhanced`). `sub_id` charset: `[a-z0-9_]`.
- Every stream carries `metadata.variant_group` (= the default stream's id) and
  `metadata.variant` (its own sub id), so a consumer groups fan streams by fields instead of
  parsing ids. `pipeline_id == variant_group` ⇔ this is the default variant.

The streams are **correlated, not identical**: one shared news walk (same cited articles, same
no-news and outage cycles — retrieval and sources are shared), but per-variant score jitter
(~95 % signal agreement, disagreement concentrated near the thresholds), per-model llm-stage
latency, per-model token and cost figures (`gpt-4o` ≈ 16× `gpt-4o-mini`), and per-variant
`LLM_TIMEOUT` cycles. Prompt provenance is identical across variants — the anchor that attributes
any score difference to the model rather than to the input.

### Path coverage

Every run exercises: `success` · `partial` (+ `SOURCE_UNREACHABLE`) · `error` (empty `result` +
`LLM_TIMEOUT`) · no-news (`HOLD` / `0.0` / `'No relevant news found'` / `[]` / `basis: no_data`) ·
breaking (`is_breaking: true`).

---

## Contracts that must not break

### The date window is bound by consumer fixtures

```
2026-04-27T00:00:00Z  →  2026-05-03T23:50:00Z      (7 days, 1008 cycles at 10 min)
```

This is not a preference. Seven configurations and test fixtures on the IDE side bind windows
*inside* this span; moving it invalidates all of them. The most sensitive is a forex demo whose
window runs **deliberately past the end of signal coverage** (to `2026-05-04T02:00`) so its tail
runs on stale data and the degradation becomes visible. If the series ends earlier or later than
`2026-05-03T23:50`, that demo loses its point.

The window also has to sit inside the consumer's tick coverage, and does: the IDE binds sentiment
to ticks by `collected_msc`, and its `kraken_spot` coverage spans `2026-01-24 … 2026-05-04`
(`mt5`: `2025-09-17 … 2026-08-14`). Data outside that range binds to nothing and no backtest runs.

### Determinism

Same seed → same signal content. This is easy to break by accident: adding a random draw anywhere
shifts every later draw and silently rewrites the whole news walk.

The pass-duration draw therefore consumes **exactly one** `random()` at a fixed position and maps
it through a piecewise inverse CDF (`_pass_seconds`) rather than drawing a component choice and a
value separately. When changing anything in the render path, preserve the number and order of
draws, or accept that the dataset changes wholesale.

### Line endings are not forced

The real archive is CRLF because the engine's server runs on Windows and Python's text mode
translates `\n`. That is a property of *that machine*, not of the format — on Linux the same
exporter writes LF. The generator writes whatever its platform writes, and the consumer's tools are
line-ending agnostic. Binding the mock to a platform accident would be worse than the
inconsistency.

---

## Keeping up with the model

The generator builds real `AnalysisEnvelope` objects, so a **structural** schema change cannot
silently drift — it fails at construction. Semantic fields are the ones that need mirroring by
hand, and the ones that have been added over time:

| Mirrored since | Fields |
|---|---|
| v0.2 (#7/#23/#24/#33/#40) | prompt provenance (`prompt_id` / `prompt_version` / `prompt_hash`), `metadata.model_snapshot` (the served dated model), run-level `prompt_tokens` / `completion_tokens` / `cost_usd` / `per_symbol_tokens`, `result[].basis` (`llm` \| `no_data` \| `degraded`; no-news rows carry `no_data` and zero tokens) |
| v0.3.2 (#85/#87) | `data_origin`; `config_fingerprint` (derived, `mock-` prefixed — see below); `metadata.trigger_reason` — `scheduled` on grid passes, `breaking` on the unscheduled ones, **never `''`** (that means "predates the field") |
| **pending** | the article `importance` tag (#3) — add here once it lands on the model |

## `config_fingerprint` — derived, never mirrored

The mock has no engine configuration to hash, so its fingerprint is computed from the generator's
own inputs (`pipeline_id`, symbols, prompt, variants, seed) and carries the same `mock-` prefix as
the prompt hash:

```
crypto_sentiment_mock       mock-1e9e9fc4
forex_macro_sentiment_mock  mock-84f6e202
```

Two rules, both learned the hard way:

- **Never mirror a real fingerprint.** Mirroring is honest for `prompt_hash` — the mock really does
  mock that prompt. Here there is nothing to mirror, so a borrowed value would be a plain lie and
  would recreate exactly the confusion the `mock-` prefix exists to end.
- **Never leave it empty.** `''` is the contract's *"produced before this field existed"*. A
  freshly generated fixture claiming that is a false statement, not a neutral one — the same trap
  `trigger_reason` had, and the reason both are stamped explicitly rather than left to their
  defaults.

Deriving it also makes the field *useful* rather than decorative: two mock runs with different
symbols get different fingerprints, so a consumer's comparability rule (`prompt_hash` **and**
`config_fingerprint` must agree) can actually be exercised instead of always matching.
