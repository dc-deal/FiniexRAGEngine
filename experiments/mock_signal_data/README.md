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
```

Flags: `--cycles --start --seed --symbols --out`. Default symbols = all 8 crypto pairs.
The short sample is `tests/fixtures/signals/` (tracked); the full week → `data/` (gitignored).

## Path coverage (both sample and week)

`success` · `partial` (+`SOURCE_UNREACHABLE`) · `error` (empty `result` + `LLM_TIMEOUT`) ·
no-news (`HOLD`/`0.0`/`'No relevant news found'`/`[]`) · breaking (`is_breaking: true`).

> Note: token/cost metric fields (#12) and the article `importance` tag (#3) are **not** in the
> mock yet — added to the model when those issues land, then mirrored here.
