# FiniexRAGEngine

[![tests](https://github.com/dc-deal/FiniexRAGEngine/actions/workflows/tests.yml/badge.svg)](https://github.com/dc-deal/FiniexRAGEngine/actions/workflows/tests.yml)

**A configurable RAG engine that turns unstructured sources into typed trading signals.**

> **Status:** Alpha · `v0.3.0-alpha` · a **live-capable, cost-safe signal producer** —
> background ingest/eval workers on independent cadences over one shared corpus
> (`--workers`), a hard budget circuit-breaker, an output-consistency guard, and a weekly
> Telegram report make an *unattended* run safe. `GET /latest` serves the persisted outcome
> instantly, `POST /run` forces a fresh pass.

FiniexRAGEngine fetches unstructured external content (news feeds, blogs, and later
event/socket streams), retrieves the relevant subset via a vector store, and asks a large
language model to distill it into a **typed, structured signal** — on a schedule. Each
configured *pipeline* declares its inputs and the signal it produces; the engine runs them
in the background and serves the latest result over a small HTTP API.

The first pipeline turns crypto news into a per-symbol **fear/greed sentiment** signal.

> 📋 **[Vision & Roadmap](https://github.com/dc-deal/FiniexRAGEngine/issues/1)** (issue #1) —
> the full vision, the phased plan, and where the build currently stands.
> 📖 **[Documentation](docs/index.md)** — architecture overview, per-stage flow maps, and
> the development/operations guides.

---

## Why a pipeline engine (not a one-off script)

The hard part of a news→signal system is the **retrieval squeeze**: there are far more
articles than fit into a prompt, they repeat across feeds, and only the recent, relevant
ones matter. FiniexRAGEngine treats this as a declarative dataflow:

```
Trigger (timeframe bar-close · breaking-wake · event-socket planned)
  └─ Pipeline (declared as a "constellation" JSON)
       ├─ Sources[]   RSS · blog · socket · API   (pluggable connectors)
       ├─ Scope       market + symbols
       ├─ RAG stage   ingest → embed → store → retrieve (top-k, recent, deduped)
       ├─ Analysis    prompt + LLM (structured output)
       └─ Outcome     typed signal (sentiment, trend, events, …)
  → persist outcome to the store
```

Adding a new signal type = adding a constellation JSON + an outcome model. The HTTP
contract and the engine stay the same.

---

## Architecture at a glance

| Layer | Responsibility | Default backend |
|---|---|---|
| Sources | fetch raw articles | RSS (feedparser) |
| Vector store | growing, deduped article corpus + similarity search | PostgreSQL + pgvector |
| Embedder | text → vector | OpenAI embeddings (local model swappable) |
| LLM provider | structured analysis | OpenAI chat-completions |
| Outcome store | source of truth for every produced signal | pgvector / JSONL |
| API | health, pipeline listing, run, latest | FastAPI |

All cross-cutting choices (vector store, embedder, LLM) sit behind small interfaces, so a
backend can be swapped without touching the pipeline logic.

The response is a **generic envelope + per-pipeline payload** — every consumer parses the
same shell regardless of the signal type:

```json
{
  "schema_version": "1.0",
  "pipeline_id": "crypto_sentiment",
  "outcome_type": "sentiment_fear_greed",
  "prompt_version": "1",
  "prompt_id": "sentiment-crypto",
  "prompt_hash": "1f191112898f",
  "timestamp": "2026-06-28T11:00:00Z",
  "status": "success",
  "result": [ { "symbol": "BTCUSD", "signal": "HOLD", "sentiment_score": 0.45, "confidence": 0.78, "reasoning": "...", "basis": "llm", "sources": [ ... ] } ],
  "metadata": { "model": "gpt-4o-mini", "articles_relevant": 23, "processing_time_ms": 1823, "cost_usd": 0.0029, "stage_timings": [ ... ] },
  "errors": []
}
```

---

## Quickstart

```bash
cp .env.example .env                # then set OPENAI_API_KEY
docker compose up -d                # pgvector PostgreSQL + pgAdmin + the engine container

docker compose exec ragengine bash  # enter the container, then build the schema:
python -m finiexragengine.cli.migrate_cli          # applies migrations/ (re-run = no-op)

python finiexragengine/cli/server_cli.py --reload --port 8100

# live mode: + background ingest/eval workers on their own cadences (continuous,
# PAID OpenAI activity — deliberate opt-in)
python finiexragengine/cli/server_cli.py --workers --port 8100
```

```bash
curl localhost:8100/v1/health
curl localhost:8100/v1/pipelines
curl -X POST localhost:8100/v1/pipelines/crypto_sentiment/run
curl localhost:8100/v1/pipelines/crypto_sentiment/latest
```

Run the tests:

```bash
pytest tests/ -v     # free suite (live API tests are excluded by default)
pytest -m paid -v    # live tests against the real OpenAI API — fractions of a cent
```

CI runs the free suite on every pull request and merge (see `.github/workflows/tests.yml`).

---

## Configuration

- `configs/app_config.json` — service-wide defaults (API, LLM model, embeddings, vector store,
  telegram + weekly report).
- `configs/pipelines/*.json` — one file per pipeline ("constellation"): sources, symbols,
  retrieval parameters, trigger, and the breaking-news threshold.

Both are validated into typed Pydantic models on load. Every level has a gitignored
**user override** (`user_configs/app_config.json`, `user_configs/pipelines/*.json`,
`user_configs/source_sets/*.json`) deep-merged onto the tracked file — machine-specific
values (secrets, a dev symbol subset, a feed switched off on this egress IP) without
touching committed config. Applied uniformly on every surface: all loading goes through
`AppConfigManager` (its constructor for the app config, its `build_*_registry()`
factories for constellations and source-sets). On startup, every applied override is
reported once as a one-liner (`[OVERRIDE] pipelines/crypto_sentiment.json ·
floor_distance 0.7→0.65 · symbols 8→2`), with typo'd keys flagged (`⚠ key?`) — a
forgotten override never steers a run silently (`logging.warn_on_override`, default on).
Full guide — merge semantics, load paths, report format:
[docs/development/user_configs_overrides.md](docs/development/user_configs_overrides.md).

The **database schema is not configuration** — it is owned by the numbered SQL files in
`migrations/` and applied with the migrate CLI, so it can evolve on a populated database
without data loss. The engine refuses to start against a schema that is behind the repo.
See [docs/development/migrations.md](docs/development/migrations.md).

---

## Inspecting the vector store (pgAdmin)

The dev stack ships a browser database admin at **http://localhost:5050** (pgAdmin,
started by `docker compose up -d`). Log in with `admin@local.dev` / `admin`; the
**FiniexRAGEngine** server is pre-registered — enter the database password `ragengine`
on first connect. You can then browse the `articles` corpus and run the pgvector
similarity queries by hand — see
[docs/development/database_inspection.md](docs/development/database_inspection.md).

**CLI tools.** Run one **full pipeline pass** (`run_cli.py` — the console twin of `POST /run`),
grow the corpus with one **ingest pass** (`ingest_cli.py` — fetch → embed → upsert, idempotent),
preview a single symbol's **evaluation** (`eval_cli.py` — signal + rendered prompt excerpt), check
per-symbol corpus **coverage** (`coverage_cli.py`), and read the **cost** and **performance**
reports (`cost_cli.py` / `perf_cli.py` — token/USD spend and API latency by section). Check **feed
health** (`sources_cli.py` — poll reliability, flag/quarantine, recent problems, orphan notice) and
diagnose a failing feed's raw output (`feed_doctor_cli.py`). Read the **weekly report** in the
console (`report_cli.py`) and **export** produced signals to the rotated JSONL archive
(`export_cli.py` — closed UTC days only, idempotent, for handover/backfill). Every paid pass ends
with a `--- run metrics ---` footer, so spend is never silent. All entries are in `.vscode/launch.json`;
details in the [DB inspection doc](docs/development/database_inspection.md#coverage-report-cli).

---

## Status

In active development — a live-capable, cost-safe signal producer. Implemented and tested
today, most load-bearing first:

- **Two-worker live service (#10, #16)**: acquisition and evaluation run as
  independently-clocked background workers over one shared corpus — ingest per
  **source-set** (declared once, referenced by N pipelines; fast and LLM-free, because RSS
  windows slide), eval per signal stream (fan-out variants included). Opt-in via
  `--workers`; every pass logs its own spend, worker states surface in `/health`. The corpus
  is **stamped with its embedding model** in the database and refuses to boot on a mismatch —
  mixed vector spaces are impossible.
- **Pipeline orchestration & a strict output contract (#7)**: `POST /run` executes the real
  staged flow — ingest → per-symbol eval → envelope assembly — honoring the contract on
  every response: **every symbol always present**, `partial` preferred over `error`,
  taxonomy-typed `RunError`s, and *always a parseable envelope* (the API answers `200` +
  `status: 'error'` on internal failure, never a bare 500). A downstream collector can parse
  every response.
- **Cost circuit-breaker (#47)**: the top risk of an unattended paid run, handled. The engine
  reacts to the provider's own spend limit — an OpenAI `insufficient_quota` at any paid seam
  **suspends paid work**, backs off, and **re-probes** on a cool-off (auto-resume); a
  suspended eval degrades to a clean `BUDGET_EXCEEDED` HOLD, a suspended ingest logs
  `suspended (quota)`, and the state shows on `/health` — no dollar-accounting to drift out
  of sync with the provider.
- **Breaking detection (#11)**: the flash-crash path. **Near-continuous ingest** (conditional
  GET, so fast polling stays cheap + polite) feeds an **LLM-free** cluster-burst + keyword
  detector that flags candidates on the corpus; a flag **wakes the eval worker out-of-band**
  (jumps the interval) at each pipeline's own sensitivity, the LLM **confirms**
  (`urgency ≥ threshold`), and a **reaction-time report** (engine vs end-to-end, from the
  store) shows the flagged→confirmed funnel. The live SSE push is the next slice (Stage C,
  IDE-accepted, paired with #9). See [breaking_detection.md](docs/architecture/breaking_detection.md).
- **Honest retrieval — the "squeeze" (#5, #24)**: two-tier top-k with a recency window,
  symbol-aware query expansion, semantic dedup before the token cap, and a min-similarity
  floor — a symbol with only off-topic coverage degrades to a clean, zero-cost `no_data`
  HOLD instead of a signal hallucinated from generic news. A per-symbol **retrieval funnel**
  in every envelope explains how each context was built. See
  [retrieval_policy.md](docs/architecture/retrieval_policy.md).
- **LLM analysis with governed, reproducible series (#6, #33, #40)**: versioned Jinja2
  prompt templates + structured OpenAI output → typed, validated per-symbol sentiment. Every
  envelope carries the prompt **content-hash fingerprint**; each pipeline declares its eval
  model behind an `allowed_models` gate, and the **served snapshot** (`response.model`) is
  recorded per call — a silent alias retarget is detected, so a signal series stays
  attributable to the exact model *and* prompt that produced it. See
  [prompt_and_llm_stage.md](docs/architecture/prompt_and_llm_stage.md).
- **Output consistency guard (#35)**: schema-valid but internally contradictory LLM rows (a
  `BUY` scored negative, a near-certain `HOLD`, an empty reasoning) are caught by a
  deterministic, zero-cost post-check and degraded to a clean `HOLD` (`partial` run, raw
  output kept) — a confidently-wrong signal never leaves the engine unmarked, and it can
  never trigger a breaking push.
- **Cost & performance, captured at the call (#23, #32)**: one billing-log row per paid API
  call (exact tokens + USD, frozen from the price table at record time) *and* its latency; a
  shared stage timer; `cost` + `perf` CLIs with per-section avg/p95/max; every spending pass
  ends with a `--- run metrics ---` footer. The store is the metrics warehouse — reports read
  from it, they are not a separate telemetry system.
- **Weekly report & Telegram alert surface (#27)**: one typed `WeeklyReport` model — cost,
  latency, source health, **no-data/coverage** (per-symbol no-data share vs the retrieval
  floor, with a calibration-candidate flag), breaking funnel, storage, and **store-derived
  worker liveness** (a silent stream reads `STALE`, no heartbeat needed) — rendered by two
  surfaces from the same numbers: the console (`report_cli`) and a **Telegram bot** (weekly
  cron + on-demand `/report`). Pure store reads, no paid calls; credentials live only in
  `user_configs/`. Each weekly run also **auto-exports** the closed-day JSONL archive
  (default on; `--no-export` skips). See [weekly_report_and_alerts.md](docs/architecture/weekly_report_and_alerts.md).
- **Outcome store & cached serving (#8, #36)**: every produced envelope is persisted
  (Postgres — the source of truth for replay and error statistics) with the **raw LLM output**
  next to it, so a run is fully reconstructable: raw output ↔ normalized result ↔ prompt
  fingerprint. `GET /latest` is an indexed read — instant, zero spend.
- **Source health & rotating logs (#11, #49)**: every poll is recorded per feed; status-aware
  fetch classifies failures (a fast loop's HTTP 429 is `RATE_LIMITED`, not a fake parse
  error), and a persistently failing feed is **flagged + quarantined** so the loop backs off.
  A **Sources report** and a **feed doctor** make a bad feed one command away; a **daily
  rotating file** log means an overnight run survives the scrollback. See
  [source_health_and_logging.md](docs/architecture/source_health_and_logging.md).
- **Foundation — corpus & embeddings (#2, #3, #4, #14, #19)**: RSS ingest into an idempotent,
  shared **pgvector** corpus (store everything, filter at retrieval); OpenAI embeddings
  (`text-embedding-3-small`, 1536 dims) with a query-vector cache; versioned **schema
  migrations** so a populated database evolves without drop-and-recreate.

Next up: the **collector handshake (#9)** + the live **SSE breaking push** (#11 Stage C). See the
full **[Vision & Roadmap](https://github.com/dc-deal/FiniexRAGEngine/issues/1)** (issue #1).

---

## Tech stack

Python 3.12 · FastAPI · Pydantic · PostgreSQL + pgvector · OpenAI API · feedparser · pytest · Docker

## Development

The codebase is built AI-assisted, pair-programming with [Claude Code](https://claude.com/claude-code)
(Anthropic Opus and Fable models). Architecture, design decisions, and code review stay human —
every change is reviewed and committed manually.

## License

MIT — see [LICENSE](LICENSE).
