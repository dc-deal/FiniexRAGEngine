# Local Database Inspection (pgAdmin)

The dev stack ships a browser-based PostgreSQL admin so you can look inside the pgvector
article corpus directly — browse rows and run the same similarity queries the retriever
runs.

## Access

`docker compose up -d` starts a **pgAdmin** container alongside Postgres.

- Open **http://localhost:5050**
- pgAdmin login: `admin@local.dev` / `admin`
- The **FiniexRAGEngine** server is pre-registered (see `.devcontainer/pgadmin_servers.json`).
  Click it and enter the database password `ragengine` on first connect.

These are local dev defaults (also in `docker-compose.yml`) — not secrets.

The corpus lives under `ragengine → Schemas → public → Tables → articles`. Right-click →
*View/Edit Data → All Rows*, or use the Query Tool.

## Browsing the corpus

```sql
SELECT title, source_id, source_weight, published_at
FROM articles
ORDER BY published_at DESC;
```

The `embedding` column is a `vector(1536)` — pgAdmin renders it as a long array; that is
expected.

## Running the similarity query by hand

pgvector's `<=>` operator is cosine distance (`0` = identical direction, ascending = most
similar first) — the same operator the retriever uses. Two ways to try it:

**Nearest neighbours of one article** (no query vector needed — this is the dedup axis:
near-duplicate stories from different feeds float to the top):

```sql
SELECT b.title, a.embedding <=> b.embedding AS distance
FROM articles a, articles b
WHERE a.article_id = '<paste an article_id>' AND b.article_id <> a.article_id
ORDER BY distance
LIMIT 10;
```

**Rank the corpus against a symbol query** — once the query-vector cache (issue #19) is
populated, the fixed query vectors live in `query_vectors` and can be joined directly, so
you never paste a 1536-number literal:

```sql
SELECT a.title, a.source_id, a.embedding <=> q.embedding AS distance
FROM articles a, query_vectors q
WHERE q.query_text = 'Bitcoin BTC'
  AND a.published_at >= now() - interval '7 days'
ORDER BY distance
LIMIT 12;
```

That join is exactly what `PgVectorStore.query` does in
`finiexragengine/core/rag/pgvector_store.py`; the Python retriever then applies the
near-duplicate collapse and the `top_k` cap on top of it (see
`../architecture/application_flow/01_ingest_and_retrieval.md`).

## Coverage report (CLI)

The manual join above answers "how well is *this* symbol covered?" one query at a time.
`finiexragengine/cli/coverage_cli.py` automates it for a whole constellation: for every
symbol query it reports the **nearest** article distance (best coverage) and the **mean**
distance across three scopes — all-time, last week, and the pipeline's recency window —
plus the **`n≤f`** count (window articles inside the relevance floor = what actually
reaches the prompt after ISSUE_24) and each scope's oldest article (`from …` — where its
counting really starts; an "all-time" over a week-old corpus is just a week).

```bash
python finiexragengine/cli/coverage_cli.py                 # crypto_sentiment (default)
python finiexragengine/cli/coverage_cli.py --pipeline forex_events --floor 0.60   # what-if tuning
```

The default floor is the pipeline's **active** `retrieval.floor_distance`, so `n≤f`
predicts real retrieval; `--floor` overrides it for tuning experiments.

Also available in the IDE as **📊 RAGEngine: Corpus Coverage Report** (`.vscode/launch.json`).
It needs `DATABASE_URL`; it runs **free** on the cached query vectors (issue #19) and only
embeds on a cache miss (`--pipeline` works for any constellation).

```
Corpus Coverage Report
config: configs/pipelines/crypto_sentiment.json  (pipeline 'crypto_sentiment')
model text-embedding-3-small | table articles | floor 0.55
corpus: 241 articles · 128 in 7d · 49 in the 1440min/24h window
--------------------------------------------------------------------------------------------
       all-time             week           window
from 07-03 09:12 from 07-03 14:00 from 07-09 15:02
   best    mean     best    mean     best    mean   n≤f  cov  symbols / query
--------------------------------------------------------------------------------------------
  0.403   0.797    0.410   0.780    0.403   0.776     3   ok  ADAUSD  "Cardano ADA"
  0.502   0.746    0.502   0.741    0.502   0.734     2   ok  BTCUSD  "Bitcoin BTC"
  0.572   0.704    0.580   0.700    0.628   0.687     0  GEN  DASHUSD  "Dash cryptocurrency"
```

Reading it:

- **best** is the distance to the nearest article — lower is better coverage. Dedicated
  symbols land around `0.40–0.50` here; a symbol with no own news drifts higher.
- **cov = GEN** flags a best-distance beyond `--floor` (default `0.55`): the corpus has no
  article close to that symbol, so retrieval would return only generic, off-topic context —
  the `HOLD` / "No relevant news found" case of the output contract.
- **window** columns are what a *live* retrieval sees right now (only articles inside the
  recency window); **week** sits between window and all-time and shows the trend (covered
  all-time but worse in week/window = the symbol has gone quiet recently); the **all-time**
  columns show the whole corpus.
- **n≤f** is the count of window articles inside the floor — the live prompt context after
  the relevance floor (ISSUE_24). `0` means retrieval comes back empty and the evaluator
  answers with the mechanical `no_data` HOLD, **skipping the LLM call entirely**.
- the **`from …`** stamps under each scope title are that scope's oldest article — the
  stats' real reach (a young corpus makes "all-time" ≈ "week"; a `from` far younger than
  the nominal window boundary reveals a feed gap).

The report is the tuning instrument for `retrieval.floor_distance` (ISSUE_24) — the same
`~0.55` cut-off that routes an uncovered symbol into the clean `no_data` HOLD path instead
of a signal hallucinated from generic news.

## Cost tracking (billing log + cost CLI)

Every paid API call (embedding news, embedding a query, later the LLM eval) writes a row to the
`cost_log` table — section, model, tokens, and the USD derived from the price table **at record
time** (frozen, so a later price change never rewrites history; the token count is the ground truth).

```bash
python finiexragengine/cli/cost_cli.py --since 7d      # or 30d, or all
```

```
Cost Report
window: last 7d
section           calls     tokens           USD
ingest_news           1        418      0.000008
ingest_query          7         20      0.000000
window total                   438      0.000009
spent (all-time): $0.000009
account credit:   not set (set cost.account_credit_usd to see remaining)
```

Reading it: embeddings cost fractions of a cent (hence the six decimals); the USD becomes meaningful
once the LLM eval runs. **Balance is derived, not fetched** — OpenAI exposes no reliable balance
endpoint, so `remaining ≈ account_credit_usd − spend`. Set your loaded credit (and an optional soft
`budget_usd`) in a **gitignored override** so it never lands in the tracked config:

```json
// user_configs/app_config.json
{ "cost": { "account_credit_usd": 50.0, "budget_usd": 20.0 } }
```

`user_configs/app_config.json` is deep-merged onto `configs/app_config.json` at load — the place for
operator-specific values and secrets. The **price table** (`pricing.models` in
`configs/app_config.json`) is hand-maintained (OpenAI has no pricing API), so update it when prices
change; past `cost_log` rows keep their as-recorded USD regardless. Browse the raw log in pgAdmin:

```sql
SELECT ts, section, model, total_tokens, usd_cost FROM cost_log ORDER BY ts DESC;
```

## Performance tracking (latency log + perf CLI)

The mirror of the cost report, from the same table: every `cost_log` row also carries the API
call's **`duration_ms`** — one capture point for cost *and* latency ("capture at the call, report
from the store"). Since a slow LLM or a hanging feed is the engine's dominant time sink, this is
where the pain points show up — and `ts + section + model + pipeline_id + duration_ms` makes a
single slow call traceable after the fact.

```bash
python finiexragengine/cli/perf_cli.py --since 7d      # or 30d, or all
```

```
Performance Report
window: last 7d
--------------------------------------------------------------
section           calls   avg ms   p95 ms   max ms    API s
--------------------------------------------------------------
llm_eval              7     2650     3100     3210     18.6
ingest_news          10      820     1400     1520      8.2
--------------------------------------------------------------
window total                                           26.8
```

Reading it: `avg` for the trend, `p95`/`max` for outliers (an API hang shows as a runaway max),
`API s` for where the wall-clock actually went. Rows recorded before latency capture existed have
no duration; they are excluded from the aggregates and counted in an `untimed legacy calls` note.

Beyond the report, every spending CLI pass (ingest, eval) echoes a `--- run metrics ---` footer —
per-stage times plus what the pass just spent — so cost and latency are visible right where the run
happened. Local per-stage timings (retrieve/prompt vs the API call) are persisted with the envelope
once `Pipeline.run` assembles `RunMetadata` (#7); the full picture is issue #32.

```sql
SELECT ts, section, model, duration_ms FROM cost_log ORDER BY duration_ms DESC NULLS LAST;
```
