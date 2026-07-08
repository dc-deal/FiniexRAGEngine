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
