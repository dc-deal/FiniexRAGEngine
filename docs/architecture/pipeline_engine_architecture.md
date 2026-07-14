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
  schema_version, pipeline_id, outcome_type, prompt_version,
  timestamp, status, result: List[T], metadata, errors
```

`SentimentEnvelope = AnalysisEnvelope[SentimentResult]` is the first concrete type. A new
signal type adds its own `result` model (e.g. `TrendResult`) and reuses the envelope.

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

The API then serves two shapes:

- `GET /v1/pipelines/{id}/latest` — the persisted outcome, served instantly (low-latency
  consumers; the IDE/collector path).
- `POST /v1/pipelines/{id}/run` — force a fresh eval pass (a guaranteed-fresh data point).

## Adding a new pipeline

1. Add a constellation JSON to `configs/pipelines/` (referencing a source-set from
   `configs/source_sets/` — add one if the feeds are new).
2. If it produces a new signal type, add its outcome `result` model.
3. The registry discovers it on startup; no engine code changes.
