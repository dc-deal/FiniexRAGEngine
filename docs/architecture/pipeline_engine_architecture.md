# Pipeline Engine Architecture

FiniexRAGEngine is a declarative, config-driven dataflow engine. This document explains the
moving parts and how a new signal type is added.

## The pipeline model

A **pipeline** is one configured unit that turns a set of sources into one typed signal. It
is declared entirely in a JSON file under `configs/pipelines/` (a "constellation"):

```
Trigger  →  Pipeline  →  [ Sources → RAG stage → Analysis → Outcome ]  →  Store
```

- **Trigger** — what drives a run. `interval` (pull every N seconds) today; `event` (push,
  e.g. a breaking-news socket) is planned. Both implement the same `start/stop` contract, so
  the pipeline does not know which one drives it.
- **Sources** — pluggable input connectors (`AbstractSource`). RSS first; blog/socket/API
  share the contract. A source returns raw `Article` objects.
- **RAG stage** — the retrieval squeeze. Articles are embedded and upserted into the vector
  store (idempotent by article id), then per query (e.g. per symbol) the top-k most similar,
  recent, deduplicated articles are retrieved. This is what keeps the prompt within budget.
- **Analysis** — a prompt is built from the retrieved context and sent to the LLM with a
  structured-output schema. The parsed result becomes the typed outcome.
- **Outcome** — a typed signal payload (`SentimentResult` first). It carries provenance: the
  article references that produced it.
- **Store** — every outcome is persisted with its timestamp. The store is the source of
  truth; the (planned) live push channel is an optimization layered on top, never the only
  record of an event.

## The generic envelope

Every pipeline returns the same shell, parameterised by its payload type:

```
AnalysisEnvelope[T]:
  schema_version, pipeline_id, outcome_type, data_origin,
  config_fingerprint, prompt_version, prompt_id, prompt_hash,
  timestamp, status, result: List[T], metadata, errors
```

`SentimentEnvelope = AnalysisEnvelope[SentimentResult]` is the first concrete type. A new
signal type adds its own `result` model (e.g. `TrendResult`) and reuses the envelope.

### Provenance: the two fingerprints (ISSUE_33 / ISSUE_85)

An envelope names both halves of what produced it, because both are series-defining and both
can change silently:

- **`prompt_hash`** fingerprints the prompt template body (front-matter excluded), so an edit
  is visible even when the version was not bumped.
- **`config_fingerprint`** fingerprints the *inputs*: the **merged** pipeline config, the
  **resolved** source set (feeds, weights, `enabled`, detection thresholds) and the
  score-defining slice of the app config (`llm.provider/temperature/base_url`,
  `embedding.model/dimensions`). Operational knobs stay out — poll intervals, timeouts, the
  ingest cadence, budgets, logging, diagnostics — so retuning a timeout never forks a series.
  `configuration/config_fingerprint.py` holds the include/exclude decisions with their reasons.

**Two archive days are comparable when both agree.** A consumer groups by
`(pipeline_id, prompt_hash, config_fingerprint)` and never aggregates across groups silently.
Both are resolved **once at assembly** and stamped by the runner, so they are valid even for a
pass where every evaluation failed. An empty value means "unknown, produced before this
existed" — never "unchanged".

A fingerprint is **not a build identifier**: two machines with different `user_configs/`
overlays legitimately produce different values for the same tracked config, because they run
different configurations (see `docs/development/user_configs_overrides.md`).

### Why a pass ran: `metadata.trigger_reason` (ISSUE_87)

The fingerprints say *what setup* produced an envelope; this says *what set the pass in motion*.
The trigger is the only unit that knows, so it passes the reason into the run — it is never
derived from the timestamp afterwards.

| Value | Produced by |
|---|---|
| `scheduled` | the planned tick — bar close (eval) or interval (ingest) |
| `boot` | the first pass after a process start, before the first wait |
| `breaking` | an out-of-band wake over the breaking bus (ISSUE_11) |
| `manual` | `run_cli` / `ingest_cli` — the operator at the console |
| `external` | `POST /v1/pipelines/{id}/run` — a foreign caller |
| `''` | unknown: produced before this field existed. **Never read as `scheduled`** |

`boot` wins over `scheduled` when a process starts on a boundary: the field names why the pass ran
*now*. The vocabulary is fixed in `types/trigger_types.py` (`TriggerReason`), but the envelope
field is a plain `str` — an archived envelope carrying a value a later version introduced must
still parse.

**`is_breaking` is not a substitute.** It is the LLM's confirmation (`urgency >= threshold`), not
the cause: measured over the real archive, 1,059 of 1,102 envelopes carrying it (96 %) are
ordinary scheduled passes that re-confirmed a lingering story. Conversely 72 of 115 off-grid
envelopes carry no breaking row at all — boot passes, manual runs, or wakes the model did not
confirm.

The reason is bound once per pass on the cost scope too, so **every `cost_log` row** the pass
produces carries it (`trigger_reason`, migration 006) — including the ingest embeddings, which
have no envelope. "What do out-of-band wakes cost us" is a `GROUP BY`. And it opens the worker's
`last_detail`, the one string the per-pass log line, the live activity stream (ISSUE_26) and
`/health` all render:

```
[eval:crypto_sentiment] breaking · success · 9 symbols (9 llm · 0 other) · 4211 tok · $0.001834 · 18213ms → outcomes
```

#### What the fingerprint does *not* answer — and why that is the design

`config_fingerprint` covers **deliberate configuration change only**. Everything that varies at
runtime stays out of it, on purpose, and is reported per envelope instead:

| Question | Where the answer is |
|---|---|
| Did the setup change between two days? | `config_fingerprint` |
| Did the prompt change? | `prompt_hash` |
| Why did this pass run at all? | `metadata.trigger_reason` (above) |
| Was a pass degraded (outage, LLM failure)? | `status`, `RunError[]` |
| Was a row scored, mechanical, or degraded? | `result[].basis` (`llm` / `no_data` / `degraded`) |
| Were feeds missing (quarantine, unreachable)? | `metadata.sources_reached` / `sources_configured` |
| Did the budget brake bite? | `status: 'partial'` + a `BUDGET_EXCEEDED` error |

The Testing IDE verified this split against the real 24-day archive: every visible anomaly there
was runtime state — 21 % degraded rows on 2026-07-29 (the infrastructure outage), 4.3 % on 07-22
(budget), 499 `no_data` rows on 07-26 (feed quarantine). **The fingerprint would have explained
none of them, and should not**: mixing runtime state into it would make it move constantly and
render it useless for the one thing it is for. The two halves are complementary — runtime
metadata explains a *day*, the fingerprint decides whether two days belong in the same *series*.

Over those 24 days the fingerprint would have moved exactly once (the 07-24 symbol expansion).
That is the expected rate: it is a forward-looking marker, and its value grows with archive
length and with #68's weekly calibration writes.

#### What it buys a downstream consumer

Beyond traceability, it is a **validity condition for multi-window experiments**. A
tuning/validation split (in-sample vs out-of-sample, parameter search) rests on the assumption
that two windows differ *only* in market conditions. Without this field, a collapsing result has
two indistinguishable explanations — the strategy was overfitted, or the signal source changed —
and the second produces a false-negative verdict: a working strategy is discarded and nobody
ever finds out. With it, that is a one-field check made *before* the result is interpreted.

It also bounds honest experiment length. Once #68 calibrates weekly, the fingerprint moves
roughly weekly, which states the maximum span of a single-regime backtest: a four-week window is
then four configuration regimes thrown together — not wrong, but no longer one experiment. And
in a consumer's own run register, a data fingerprint next to their strategy fingerprint makes
the register self-delimiting: same strategy, different data identity, mechanically separated
instead of remembered.

**Its limit, stated plainly:** to a consumer the value is opaque — it says *that* something
changed, never *what*. The "what" lives in `config_fingerprints` on the engine side (below), so
an unexplained series break is one question to the engine, not a research project.

### `config_fingerprints` — what a fingerprint stood for

The scalar says *that* a setup changed; the registry says *what* it was. `build_runner` upserts
one row per distinct configuration — `fingerprint` (primary key), `pipeline_id`,
`source_set_id`, the canonical `config` payload as JSONB, `first_seen`, `last_seen` (boot-scoped,
not per pass). No retention: the table must outlive the archive it explains. A failed write is
logged and swallowed — the envelope's fingerprint does not depend on it.

```sql
SELECT fingerprint, pipeline_id, first_seen, last_seen FROM config_fingerprints ORDER BY first_seen;
SELECT jsonb_pretty(config) FROM config_fingerprints WHERE fingerprint = '904c2e16bbfb';
```

At boot each assembled pipeline reports its fingerprint, right behind the `[OVERRIDE]` lines —
first what diverges, then what follows from it. `(new)` means this start breaks the comparable
series:

```
[CONFIG] crypto_sentiment · source_set crypto_news · config_fingerprint 904c2e16bbfb
[CONFIG] forex_macro_sentiment · source_set forex_news · config_fingerprint 1e63b9aa21fc (new)
```

#### A fingerprint's `first_seen`/`last_seen` is not its span

The two columns invite being read as an interval, and that reading invents history. A configuration
can appear, be **reverted**, and return later — so a fingerprint's first and last sighting can
straddle a long stretch in which it produced nothing at all. Taking min and max then makes two
generations look like two *concurrent* ones.

Worked example, 2026-08-25 on the production crypto stream. `3cce880a58d4` (6 active feeds) and
`9458492ce234` (7 — the same six plus `theblock`) appear to overlap: B's `first_seen` sits eight
minutes inside A's span and runs to the present. They never overlapped. Five restarts happened
around one deploy, and B produced **exactly one pass**:

```
16:18:45  3cce880a58d4  boot        deploy: source breadth, theblock still out
16:26:14  9458492ce234  boot        theblock in — one pass
16:28:39  3cce880a58d4  boot        reverted
   …      3cce880a58d4  scheduled   for the next 82 minutes
17:54:40  9458492ce234  boot        applied for good; still current
```

**How to tell the two apart, and why the archive settles it.** Concurrency would interleave: two
live assemblies minting into one stream produce passes whose `seq` values do not ascend with their
timestamps. They did — strictly, in both streams, with no duplicates. And the second stream is the
control: the same five boots left `forex_macro_sentiment`'s fingerprint untouched, which localises
the differing leaf to the crypto source set without reading a single config payload. The registry
then names it (`theblock`) rather than being asked to find it.

**And do not mistake that ordering test for a detector.** It rules concurrency *out*; it cannot find
the excursion. Nothing about the ordering here is wrong — one generation per boot, in strict
succession — so no ordering check would ever flag a configuration that appeared for a single pass
and was taken back. Only looking at the gaps finds that.

So: group by fingerprint **and look at the gaps**. `first_seen`/`last_seen` answer "when was this
setup ever alive", never "was it alive throughout". It is the same failure as a single confirm rate
per prompt version (`prompt_drift`, ISSUE_110): a summary that hides the distribution it summarises.

## Interfaces (swappable backends)

| Interface | Default | Swap candidates |
|---|---|---|
| `AbstractSource` | `RssSource` | blog, socket, API connectors |
| `AbstractEmbedder` | `OpenAIEmbedder` | local sentence-transformers |
| `AbstractVectorStore` | `PgVectorStore` | Chroma, Qdrant |
| `AbstractLLMProvider` | `OpenAIProvider` | any OpenAI-format backend (vLLM, Ollama) |
| `AbstractTrigger` | `IntervalTrigger` | event/push trigger |

## Serving model — the two-worker split (ISSUE_10)

Acquisition and evaluation are separate, independently-clocked background workers over
the one shared corpus, started opt-in via `server_cli --workers` (continuous paid
activity is a deliberate choice; without the flag the server is a free, passive API):

- **Ingest workers** — one per *referenced* **source-set**
  (`configs/source_sets/<id>.json`: feeds + ingest cadence; declared once, referenced by
  constellations via `source_set`). Fast, LLM-free: fetch (conditional GET, near-continuous
  ~15s) → embed only new → upsert → **flag breaking candidates** (`BreakingDetector`, no LLM,
  ISSUE_11). One set feeds every pipeline referencing it (1× fetch, N× read). Each poll is
  **health-tracked** (`source_health`): status-aware fetch (a fast loop's HTTP 429 is
  `RATE_LIMITED`, not a fake parse error), and a persistently failing feed is flagged and
  quarantined so the loop backs off — see
  [`source_health_and_logging.md`](source_health_and_logging.md).
- **Eval workers** — one per logical pipeline (fan-out variants included, ISSUE_42), on
  the constellation's `trigger` cadence (default 600s) **or a breaking wake** (`EventTrigger`
  + `BreakingBus`, ISSUE_11 — a flagged candidate at/above the pipeline's `breaking.min_importance`
  jumps the queue in seconds): retrieve → LLM → assemble → persist (`OutcomeStore`, ISSUE_8). In
  worker mode the runners are **ingest-less** — `/run` cannot double-ingest next to a running worker.
- Every pass logs one compact line incl. its spend (cost is never silent) to the console
  **and a daily-rotating file** (`logs/finiex.log`, so an overnight run survives the
  scrollback; ISSUE_11); worker states (last run, status, run count) surface in
  `GET /v1/health`. A failing pass is logged and the loop continues — the next tick heals.

### The workers are independent — deliberately (ISSUE_74)

Passes are **not** serialized against each other. They used to be, by one `asyncio.Lock` shared
across every worker: cheap at these cadences, and it kept two invariants safe for free. It also
meant that a pass which never *returned* held every other worker hostage — and on 2026-08-01 one
did, when an un-timeouted TLS handshake (ISSUE_73) blocked a thread for nine days and took the
whole engine with it. `asyncio.to_thread` cannot be cancelled, so the lock was never released.

The two invariants now live where they belong:

- **Cost attribution** — `CostRecorder.pass_scope()` accumulates only the calls made inside it,
  carried into the worker thread by the context copy `asyncio.to_thread` makes. It replaced a
  session delta against the shared recorder, which was only correct under serialization. This is
  what makes `metadata.cost_usd` on every persisted envelope trustworthy without a lock.
- **The live counters** — `EngineStats` owns a small lock for its read-modify-write writers (see
  `live_display.md`).

What replaces the lock per worker is a **deadline**, not another lock: `pass_timeout_seconds`
(default 300). A pass that overruns it is abandoned and the worker resumes on its next tick
instead of staying dead until a restart. Note what that does and does not do — `asyncio.wait_for`
abandons the *await*, not the thread, so a blocked thread keeps running and holds an executor
slot. It bounds the damage rather than undoing it. The value sits deliberately **below** the stall
watchdog's floor (ISSUE_75): the engine gets a chance to heal itself before it raises its voice.

No per-worker lock was added either, because none is needed: `IntervalTrigger` and `EventTrigger`
both await the pass before computing the next wait, so a worker cannot overlap itself.

One consequence worth knowing when reading the performance report: passes now genuinely run
concurrently, so `duration_ms` samples are contention-sensitive in a way they were not before.
Earlier measurements were taken under artificial exclusivity.

The API then serves two shapes:

- `GET /v1/pipelines/{id}/latest` — the persisted outcome, served instantly (low-latency
  consumers; the IDE/collector path).
- `POST /v1/pipelines/{id}/run` — force a fresh eval pass (a guaranteed-fresh data point).

## Adding a new pipeline

1. Add a constellation JSON to `configs/pipelines/` (referencing a source-set from
   `configs/source_sets/` — add one if the feeds are new).
2. If it produces a new signal type, add its outcome `result` model.
3. The registry discovers it on startup; no engine code changes.
