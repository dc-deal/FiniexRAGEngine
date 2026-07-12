# Mock Signal Data (experiment)

Generates JSONL that mimics the **FiniexDataCollector** archive of FiniexRAGEngine sentiment
envelopes, so **FiniexTestingIDE (#141)** can be built/tested before the real collector runs.
The sample was validated by the IDE as contract-conformant.

**Not for correctness** — plausible, schema-valid mock data only. The format may still change.

## Line format (validated against the IDE contract)

One JSONL line per ~10-min snapshot (the §3 handoff form):

- the full `AnalysisEnvelope` (typed by `finiexragengine.types.outcome_types`), one
  `SentimentResult` per symbol, plus
- a top-level **`collected_msc`** = **epoch milliseconds (UTC int)** = the collector's receive
  time = the IDE merge key (nearest snapshot with `collected_msc <= tick.collected_msc`; no
  look-ahead). The RAG engine does not set this — the mock stamps it; the live collector stamps
  the real sub-second receive time.

## Date window (hard constraint)

The IDE binds sentiment to ticks by `collected_msc`, so the data **must fall inside their
kraken_spot tick coverage `2026-01-24 .. 2026-05-04`**. Default start = **`2026-04-27`**
(recommended window `2026-04-27 .. 2026-05-03`); 1008 cycles = exactly 7 days.

## Run (from repo root)

```bash
# 5-cycle fixture sample (default → tests/fixtures/signals/), covers all paths
python experiments/mock_signal_data/generate.py

# the full week (1008 snapshots, in-window) → gitignored data/
python experiments/mock_signal_data/generate.py --cycles 1008 --out data/mock_signals/full_week.jsonl

# variant fan-out week (#42): two model streams, one file each → data/mock_signals/variant_week/
python experiments/mock_signal_data/generate.py --cycles 1008 \
    --variants "mini=gpt-4o-mini,4o_enhanced=gpt-4o" --out data/mock_signals/variant_week
```

Flags: `--cycles --start --seed --symbols --out --variants`. Default symbols = all 8 crypto
pairs. The short sample is `tests/fixtures/signals/` (tracked); the weeks → `data/` (gitignored).

## Variant fan-out week (#42 — format A, confirmed by the IDE)

`--variants "sub_id=model,..."` renders one constellation through N mock models as **separate
streams** — the naming the IDE confirmed on 2026-07-11:

- The **first** entry is the default variant and **keeps the bare `pipeline_id`**
  (`crypto_sentiment`); the others get `<pipeline_id>_<sub_id>` (`crypto_sentiment_4o_enhanced`).
  `sub_id` charset: `[a-z0-9_]`.
- **Every** stream carries the grouping hints `metadata.variant_group` (= the default stream's
  id) and `metadata.variant` (its sub id). These fields land in `RunMetadata` when #42 ships —
  the mock **previews** them here for the IDE's format validation. `pipeline_id ==
  variant_group` ⇔ default variant.
- The streams are **correlated, not identical**: one shared news walk (same `collected_msc`
  per cycle, same cited articles, same no-news/partial cycles — retrieval and sources are
  shared), but per-variant score jitter (~95% signal agreement, disagreement near thresholds),
  per-model llm-stage latency, per-model token/cost figures (`gpt-4o` ≈ 16× `gpt-4o-mini`),
  and per-variant `LLM_TIMEOUT` cycles. Prompt provenance (`prompt_id`/`prompt_hash`) is
  identical across variants — the anchor that attributes score differences to the model.

## Path coverage (both sample and week)

`success` · `partial` (+`SOURCE_UNREACHABLE`) · `error` (empty `result` + `LLM_TIMEOUT`) ·
no-news (`HOLD`/`0.0`/`'No relevant news found'`/`[]`) · breaking (`is_breaking: true`).

> Mirrored from the model since v0.2 (landed with #7/#23/#24/#33/#40): prompt provenance
> (`prompt_id`/`prompt_hash`, `prompt_version: '2'`), `metadata.model_snapshot` (the served
> dated model), run-level token/cost fields (`prompt_tokens`/`completion_tokens`/`cost_usd`/
> `per_symbol_tokens`), and `result[].basis` (`'llm' | 'no_data' | 'degraded'`; no-news rows
> carry `'no_data'` + zero tokens). Still pending: the article `importance` tag (#3) — added
> to the model when it lands, then mirrored here.
