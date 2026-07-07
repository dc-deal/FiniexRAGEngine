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

## Serving model

Pipelines run in the background on their trigger and write the latest outcome to the store.
The API then serves two shapes:

- `GET /v1/pipelines/{id}/latest` — the cached outcome, served instantly (low-latency consumers).
- `POST /v1/pipelines/{id}/run` — force a fresh run (a guaranteed-fresh data point).

## Adding a new pipeline

1. Add a constellation JSON to `configs/pipelines/`.
2. If it produces a new signal type, add its outcome `result` model.
3. The registry discovers it on startup; no engine code changes.
