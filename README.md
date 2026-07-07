# FiniexRAGEngine

[![tests](https://github.com/dc-deal/FiniexRAGEngine/actions/workflows/tests.yml/badge.svg)](https://github.com/dc-deal/FiniexRAGEngine/actions/workflows/tests.yml)

**A configurable RAG engine that turns unstructured sources into typed trading signals.**

> **Version:** 0.1.0
> **Status:** Alpha — Phase 1 vertical slice in progress

FiniexRAGEngine fetches unstructured external content (news feeds, blogs, and later
event/socket streams), retrieves the relevant subset via a vector store, and asks a large
language model to distill it into a **typed, structured signal** — on a schedule. Each
configured *pipeline* declares its inputs and the signal it produces; the engine runs them
in the background and serves the latest result over a small HTTP API.

The first pipeline turns crypto news into a per-symbol **fear/greed sentiment** signal.

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
  "timestamp": "2026-06-28T11:00:00Z",
  "status": "success",
  "result": [ { "symbol": "BTCUSD", "signal": "HOLD", "sentiment_score": 0.45, "confidence": 0.78, "reasoning": "...", "sources": [ ... ] } ],
  "metadata": { "model": "gpt-4o-mini", "articles_relevant": 23, "processing_time_ms": 1823, "stage_timings": [ ... ] },
  "errors": []
}
```

---

## Quickstart

```bash
cp .env.example .env          # then set OPENAI_API_KEY
docker compose up -d          # starts the engine + a pgvector PostgreSQL

# inside the container:
python finiexragengine/cli/server_cli.py --reload --port 8100
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

## Status

In active development. Implemented and tested today:

- **RSS ingest** into an idempotent, shared **pgvector article corpus** (store everything,
  filter at retrieval).
- **OpenAI embeddings** (`text-embedding-3-small`, app-wide, 1536 dims).
- **Retrieval stage**: two-tier top-k with recency window, symbol-aware query expansion,
  and semantic dedup before the token cap.

Next up: the LLM analysis stage (prompt builder + structured output) and full pipeline
orchestration — the API serves a typed mock envelope until those land. See the roadmap
in issue #1.

---

## Tech stack

Python 3.12 · FastAPI · Pydantic · PostgreSQL + pgvector · OpenAI API · feedparser · pytest · Docker

## Development

The codebase is built AI-assisted, pair-programming with [Claude Code](https://claude.com/claude-code)
(Anthropic Opus and Fable models). Architecture, design decisions, and code review stay human —
every change is reviewed and committed manually.

## License

MIT — see [LICENSE](LICENSE).
