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
`../architecture/detailed_ingest_and_retrieval.md`).

## Coverage report (CLI)

The manual join above answers "how well is *this* symbol covered?" one query at a time.
`finiexragengine/cli/coverage_cli.py` automates it for a whole constellation: for every
symbol query it reports the **nearest** article distance (best coverage) and the **mean**
distance, both all-time and within the pipeline's recency window, plus the corpus size.

```bash
python finiexragengine/cli/coverage_cli.py                 # crypto_sentiment (default)
python finiexragengine/cli/coverage_cli.py --pipeline forex_events --floor 0.55
```

Also available in the IDE as **📊 RAGEngine: Corpus Coverage Report** (`.vscode/launch.json`).
It needs `DATABASE_URL`; it runs **free** on the cached query vectors (issue #19) and only
embeds on a cache miss (`--pipeline` works for any constellation).

```
Corpus Coverage Report
config: configs/pipelines/crypto_sentiment.json  (pipeline 'crypto_sentiment')
model text-embedding-3-small | table articles
corpus: 87 articles (46 within the 1440min/24h window)
--------------------------------------------------------------------------
     all-time          window
  best   mean     best   mean  cov  symbols / query
--------------------------------------------------------------------------
 0.403  0.797    0.403  0.776   ok  ADAUSD  "Cardano ADA"
 0.502  0.746    0.502  0.734   ok  BTCUSD  "Bitcoin BTC"
 0.572  0.704    0.572  0.687  GEN  DASHUSD  "Dash cryptocurrency"
```

Reading it:

- **best** is the distance to the nearest article — lower is better coverage. Dedicated
  symbols land around `0.40–0.50` here; a symbol with no own news drifts higher.
- **cov = GEN** flags a best-distance beyond `--floor` (default `0.55`): the corpus has no
  article close to that symbol, so retrieval would return only generic, off-topic context —
  the `HOLD` / "No relevant news found" case of the output contract.
- **window** columns are what a *live* retrieval sees right now (only articles inside the
  recency window); the **all-time** columns show the whole corpus. A symbol covered all-time
  but `n/a`/worse in the window has gone quiet recently.

The report is the empirical companion to a retrieval **min-similarity floor** — the same
`~0.55` cut-off that would route an uncovered symbol into the clean `HOLD` path instead of a
signal hallucinated from generic news.
