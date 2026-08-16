# Mock Generator — calibration against real data

Every timing and rate constant in `experiments/mock_signal_data/generate.py` is derived from
measurements of the production archive, not chosen for plausibility. This document records the
measurements, the reasoning, and what still diverges.

**Read this before changing a distribution.** Several constants look arbitrary and are not.

Reference measurement: four clean days of `crypto_sentiment`, 2026-08-11 → 08-14, containing no
outage (629 envelopes, 5,661 symbol results).

---

## 1. Two mechanisms, not one distribution

The first version of the time model got this wrong twice in a row, so it is worth stating plainly.

```
Envelopes:                     629  =  157.2 / day        a pure M10 grid would give 144 / day
Gaps between envelopes:        p05 = 159s   p50 = 600s   p95 = 604s
Gaps < 300s:                    53  =  13.2 / day        → ADDITIONAL envelopes between bars
```

| | Rate | What it is |
|---|---|---|
| **A — scheduled pass** | 144/day, on the M10 grid | distance to the bar close = **processing time** |
| **B — unscheduled envelope** | ~13/day (+9 %) | sits **between** two bars; the distance is **not** a delay |

For a B envelope, "+583s after the bar close" does not mean anything was slow — **the envelope does
not belong to that bar at all.** The engine evaluates between bars when a breaking candidate jumps
the eval queue, and a restart also produces one boot pass outside the grid.

Modelling the tail as "sometimes a pass is very slow" would encode a false causal story into the
data. They are generated separately: `_pass_seconds()` for A, a fixed offset handed to
`_render_variant(..., fixed_offset_s=…)` for B.

### The history, so it is not repeated

| Version | `collected_msc` | `timestamp` | Problem |
|---|---|---|---|
| original | exact bar close | bar close **− 2s** | a signal dated *ahead of its own bar* reads as look-ahead in a backtest — the one error direction a backtest tool must never have |
| second | = `timestamp` | bar close **+ 10–25s** | head correct, but capped at 30s: modelled a producer that is *always* fast, so latency-sensitive paths were never exercised |
| current | = `timestamp` | A: bar close + skewed duration · B: own position in the bar | — |

`collected_msc == timestamp` exactly, including the sub-millisecond truncation of
`int(ts.timestamp() * 1000)`. There is no collector yet, so the real exporter derives one from the
other; the mock mirrors that. When #9 lands and a genuine receive time exists, **both sides change
together.**

---

## 2. Mechanism A — the pass-duration distribution

### Targets and result

| | p50 | p95 | max | floor | ≤ 60s |
|---|---|---|---|---|---|
| real | 17.4s | 66.2s | 583.4s | 3.1s | 94.6 % |
| target | 16s | 50s | 580s | 5s | ~95 % |
| **`_pass_seconds`, 50k draws** | **16.0s** | **49.8s** | **579.8s** | **5.0s** | **95.3 %** |

### Why a mixture and not one distribution

A single log-normal wide enough to produce a 580s outlier drags its own median far past 16s; one
narrow enough to hold the median never produces the tail at all. So:

- **95 %** — log-normal around the median (`PASS_BODY_SIGMA = 0.629`, so the body's own p95 lands
  at ~2.8× the median), floored at 5s.
- **5 %** — a curved tail from the body edge to the cap.

Two subtleties that cost a debugging round each:

1. **The body uses `u` directly as the log-normal's quantile**, not `u / 0.95`. Rescaling lets the
   body run out to its *own* extreme quantiles (~315s) and the mixture then overshoots p95 twice
   over — measured 88.6s instead of 50s.
2. **The tail is curved (`PASS_TAIL_CURVE = 1.5`), not linear.** A linear rise puts too much mass
   just above the body edge and drags the overall p95 from 50s to 68s.

### Why the cap is exactly 580s

The consumer flags a **gap** when two envelopes are more than 2× the measured cadence apart
(> 1200s). The worst case is a floor-value envelope followed by a tail-value one:

```
600 + 580 − 5 = 1175s        25s of margin
600 + 600 − 5 = 1195s        5s of margin — too thin
```

And the second, independent reason: the last bar of the window closes at 23:50. At +580s the
envelope lands at 23:59:40 and stays in the `2026-05-03` bucket. At +600s it would spill into a
**eighth day file** on 05-04, and a consumer fixture that depends on coverage ending at 23:50 would
lose its point.

Mechanism B cannot create a gap — an extra envelope only ever *shortens* the distance between two
others — so the cap concerns A alone.

---

## 3. Mechanism B — unscheduled envelopes

`EXTRA_ENVELOPE_RATE = 13.2 / 144` (~9 % of cycles) produces one extra envelope, placed uniformly
in `EXTRA_MIN_S … EXTRA_MAX_S` (60–540s) after the bar close. It carries the same news state (it is
the same ten minutes) with its own timestamp and its own breaking reading.

Measured result: 13.1/day (crypto), 15.7/day (forex) against ~13.2 real.

---

## 4. `is_breaking` — the largest divergence found so far

```
Share of envelopes with is_breaking = True
   real :  35.5 %      (223 of 629)          per symbol result: 6.7 %
   mock :   1.3 %      (13 of 1008)          ← before this calibration
```

A factor of 27. The dataset could not exercise anything that reacts to breaking news: 13 events per
week across eight symbols is not a test case.

**Breaking is a story, not a symbol.** A hot crypto headline moves several tickers at once — real
data shows **1.69 breaking symbols per breaking envelope**. So a cycle either breaks or does not
(`BREAKING_CYCLE_RATE = 0.35`), and then hits 1–3 symbols. The old model drew one symbol with a
1.5 % chance, which produced neither the rate nor the correlation.

Result: **33.0 %** of envelopes and **7.1 %** of results (real: 35.5 % / 6.7 %).

### `is_breaking` is content, not scheduling

Worth stating because it is counter-intuitive: a perfectly ordinary scheduled pass reports
`is_breaking=True` when a story happens to be hot. "Breaking envelope" and "unscheduled envelope"
are two different things that only partly overlap:

```
envelopes > 120s from the bar:  91  —  of which breaking: 44  (48 %)
unscheduled envelopes (B):      53  —  of which breaking: 23  (43 %)

is_breaking = False :  n=1661   p50 = 16.0s   p95 =  50.7s   max = 583.1s
is_breaking = True  :  n= 702   p50 = 18.0s   p95 = 173.2s   max = 583.4s
```

Breaking envelopes are **not** the fastest — same median, markedly higher p95. Keeping outliers
away from breaking cycles (an early proposal) would have skewed the data the other way.

---

## 5. Known divergences

These are open on purpose. Each is a decision, not an oversight.

### The forex mock is far more eventful than real forex

```
real forex_macro_sentiment, 2026-08-11 → 08-14:   0.0 % of envelopes breaking, 0.0 % of results
```

The 35 % target is a **crypto** property. With eight symbols it implies 6.7 % per result — a match.
With **two** symbols, reaching the same envelope rate forces **28 %** of results to break. The forex
mock is therefore four times more eventful than real crypto, and infinitely more than real forex.

It was built to the consumer's stated target (which did not distinguish pipelines) because their
forex fixture targets staleness rather than breaking. If faithfulness is wanted instead, scale the
story rate by symbol count so the *result* rate lands at ~6.7 % — the envelope rate for a 2-symbol
stream then falls to ~13 %.

### Gap-based detection of B cannot be exact

A consumer that identifies B envelopes as *"gap to predecessor < 300s"* will misclassify: when a
**scheduled** pass takes 580s, the next scheduled envelope is only ~36s behind it and looks exactly
like a B envelope.

The two requirements — *A may reach 580s* and *B is detected via gaps* — cannot both hold. Visible
in the output: crypto measures 13.1/day (right), forex 15.7/day (inflated by false positives). The
same limitation applies to the measurement of the real archive that produced the 13.2/day figure.

Resolving it needs an explicit marker on the envelope (a `trigger: scheduled | wake` field), which
is an engine change, not a generator one.

### Cadence median is 597s, not exactly 600s

The skewed A offsets jitter the gaps slightly. A consumer that derives its gap threshold from the
*measured* median gets 1194s instead of 1200s; the largest observed gap is 1169s (forex), leaving
24s. If that is too tight, lower `PASS_TAIL_MAX_S` from 580 to 540.

### The envelope count is no longer `--cycles`

Mechanism B adds envelopes on top of the grid, so `--cycles 1008` produces **~1091–1099** envelopes,
matching real data's 157/day. Any consumer check expecting exactly 1,008 snapshots needs updating.

---

## 6. Verification recipe

After regenerating, measure these five against the table above:

1. **Envelopes per day** — target ~157 (grid 144 + B).
2. **A distribution** — p50 / p95 / max / floor. Measure `_pass_seconds()` directly over ≥ 10k
   draws rather than from the file: a file-level split of A and B is approximate (see above).
3. **B rate** — envelopes whose gap to the predecessor is < 300s, ~13/day.
4. **`is_breaking`** — share of envelopes (~35 %) *and* share of results (~6.7 %). Both, always:
   the two can only be read together, since one is a function of the symbol count.
5. **No gaps** — no distance between consecutive envelopes above 2× the measured median.

And the provenance triple on the first line: `data_origin: synthetic`, a `*_mock` `pipeline_id`,
and a `mock-`-prefixed `prompt_hash`.
