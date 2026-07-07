# Testing

## Running

```bash
pytest tests/ -v        # free suite — no API cost, paid tests are excluded by default
pytest -m paid -v       # live tests against the real OpenAI API (fractions of a cent)
```

The default exclusion comes from `pytest.ini` (`addopts = -m "not paid"`). The
PostgreSQL-backed tests skip themselves when no database is reachable, so the free
suite is green everywhere.

## Cost fencing (`paid` marker)

Tests that spend real API budget carry the `paid` marker and live in clearly-named
`*_live.py` files. They are excluded from every default run and from CI; they exist to
validate what mocks cannot: authentication, model behavior, semantic quality. Run them
deliberately via `pytest -m paid` or the 💸 launch entry in `.vscode/launch.json`.

## Suites

| File | Covers | Needs |
|---|---|---|
| `test_api_health.py` | API contract: health, pipeline listing, run envelope | — |
| `test_rss_source.py` | RSS → Article mapping, idempotent ids | — |
| `test_openai_embedder.py` | batching, order preservation, dimension guard (mocked client) | — |
| `test_pgvector_store.py` | idempotent upsert, recency/similarity query, importance filter | PostgreSQL |
| `test_retriever.py` | two-tier policy, top_k cap, near-dup collapse, tie-breaks (mocked) | — |
| `test_symbol_query_map.py` | constellation alias + base-currency fallback | — |
| `test_rag_live.py` 💸 | real embeddings end-to-end through store + retriever | `OPENAI_API_KEY` + PostgreSQL, `-m paid` |

## Continuous integration

`.github/workflows/tests.yml` runs the free suite on every pull request and on every
push to `master`, against a pgvector service container — the database-backed tests run
for real in CI. Paid tests never run in CI.
