# FiniexRAGEngine

[![tests](https://github.com/dc-deal/FiniexRAGEngine/actions/workflows/tests.yml/badge.svg)](https://github.com/dc-deal/FiniexRAGEngine/actions/workflows/tests.yml)

**A configurable RAG engine that turns unstructured sources into typed trading signals.**

> **Status:** Alpha · `v0.2.0-alpha` · the engine runs as a live service — background
> ingest/eval workers on independent cadences over one shared corpus (`--workers`),
> `GET /latest` serves the persisted outcome instantly, `POST /run` forces a fresh pass

FiniexRAGEngine fetches unstructured external content (news feeds, blogs, and later
event/socket streams), retrieves the relevant subset via a vector store, and asks a large
language model to distill it into a **typed, structured signal** — on a schedule. Each
configured *pipeline* declares its inputs and the signal it produces; the engine runs them
in the background and serves the latest result over a small HTTP API.

The first pipeline turns crypto news into a per-symbol **fear/greed sentiment** signal.

> 📋 **[Vision & Roadmap](https://github.com/dc-deal/FiniexRAGEngine/issues/1)** (issue #1) —
> the full vision, the phased plan, and where the build currently stands.

---

## Why a pipeline engine (not a one-off script)

The hard part of a news→signal system is the **retrieval squeeze**: there are far more
articles than fit into a prompt, they repeat across feeds, and only the recent, relevant
ones matter. FiniexRAGEngine treats this as a declarative dataflow:

```
Trigger (interval now · event/push planned)
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

docker compose exec ragengine bash  # enter the container, then start the API:
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

- `configs/app_config.json` — service-wide defaults (API, LLM model, embeddings, vector store).
- `configs/pipelines/*.json` — one file per pipeline ("constellation"): sources, symbols,
  retrieval parameters, trigger, and the breaking-news threshold.

Both are validated into typed Pydantic models on load.

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
reports (`cost_cli.py` / `perf_cli.py` — token/USD spend and API latency by section). Every paid
pass ends with a `--- run metrics ---` footer, so spend is never silent. All entries are in
`.vscode/launch.json`; details in the
[DB inspection doc](docs/development/database_inspection.md#coverage-report-cli).

---

## Status

In active development. Implemented and tested today:

- **RSS ingest** into an idempotent, shared **pgvector article corpus** (store everything,
  filter at retrieval).
- **OpenAI embeddings** (`text-embedding-3-small`, app-wide, 1536 dims).
- **Retrieval stage**: two-tier top-k with recency window, symbol-aware query expansion,
  semantic dedup before the token cap, and a min-similarity floor — a symbol with only
  off-topic coverage degrades to a clean, zero-cost `no_data` HOLD instead of a signal
  hallucinated from generic news (#24).
- **LLM analysis stage**: versioned prompt templates + structured OpenAI output — typed,
  validated per-symbol sentiment (#6), with prompt **metadata + content-hash fingerprint**
  recorded in every envelope (#33).
- **Pipeline orchestration**: `POST /run` executes the real staged flow — ingest → per-symbol
  eval → envelope assembly honoring the output contract (every symbol always present,
  `partial` over `error`, taxonomy-typed errors, always a parseable envelope) (#7).
- **Cost & performance tracking**: a per-call token/USD **and latency** billing log, `cost` +
  `perf` CLIs, per-stage timings assembled into every envelope (#23, #32).
- **Model governance & exact-model tracking**: each pipeline declares its eval model (required)
  behind an `allowed_models` gate; alias models (`gpt-4o-mini`) are allowed for convenience while
  the **served snapshot** (`response.model`, e.g. `gpt-4o-mini-2024-07-18`) is recorded per call
  and per envelope — a silent alias retarget is detected and warned, so signal series stay
  attributable to the exact model (and prompt) that produced them (#40, #33).

- **Outcome store & cached serving**: every produced envelope is persisted (Postgres — the
  source of truth for replay and error statistics) with the **raw LLM output** stored next to
  it, so a run is fully reconstructable: raw output ↔ normalized result ↔ prompt fingerprint
  (#8, #36). `GET /latest` is an indexed read — instant, zero spend.
- **Two-worker live service**: acquisition and evaluation run as independently-clocked
  background workers over one shared corpus (#10) — ingest per **source-set** (declared once,
  referenced by N pipelines; fast and LLM-free, because RSS windows slide), eval per signal
  stream (fan-out variants included). Opt-in via `--workers`; every pass logs its own spend,
  worker states surface in `/health`. The corpus is **stamped with its embedding model** in the
  database and refuses to boot on a mismatch (#16) — mixed vector spaces are impossible.
- **Breaking detection (#11)**: the flash-crash path. **Near-continuous ingest** (conditional GET,
  so fast polling stays cheap + polite) feeds an **LLM-free** cluster-burst + keyword detector that
  flags candidates on the corpus; a flagged candidate **wakes the eval worker out-of-band** (jumps
  the interval) at each pipeline's own sensitivity, the LLM **confirms** (`urgency ≥ threshold`),
  and a **reaction-time report** (engine vs end-to-end, from the store) shows the flagged→confirmed
  funnel. The live SSE push wire is the next slice (Stage C, IDE-accepted, paired with #9).

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
