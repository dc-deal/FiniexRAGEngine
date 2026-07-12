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

The API contract tests build the app with `create_app(attach_runners=False)` — pinned to
scaffold-mock mode. A real runner behind `/run` makes paid calls, so the free suite must
never attach one just because `DATABASE_URL`/`OPENAI_API_KEY` are set in the environment.

## Suites

| File | Covers | Needs |
|---|---|---|
| `test_api_health.py` | API contract: health, pipeline listing, run envelope (mock mode) | — |
| `test_rss_source.py` | RSS → Article mapping, idempotent ids | — |
| `test_openai_embedder.py` | batching, order preservation, dimension guard (mocked client) | — |
| `test_pgvector_store.py` | idempotent upsert, recency/similarity query, importance filter | PostgreSQL |
| `test_retriever.py` | two-tier policy, top_k cap, near-dup collapse, tie-breaks (mocked) | — |
| `test_symbol_query_map.py` | constellation alias + base-currency fallback | — |
| `test_query_vector_cache.py` | cached query vectors, cache busting on config/model change | PostgreSQL |
| `test_ingestor.py` | fetch → skip known ids → embed only new → upsert; per-source counts | — |
| `test_coverage_report.py` | corpus coverage aggregation + console rendering | PostgreSQL |
| `test_prompt_builder.py` | Jinja2 `.md` fill + versioning; front-matter metadata + body hash | — |
| `test_pipeline_prompt_config.py` | pipeline-declared `prompt` block (name + version) | — |
| `test_sentiment_llm_output.py` | strict scored-subset schema (ranges, forbid extras) | — |
| `test_openai_provider.py` | structured call, error taxonomy mapping, cost capture (mocked) | — |
| `test_symbol_evaluator.py` | retrieve → prompt → LLM → enrich; provenance, raw-output capture | — |
| `test_pipeline_runner.py` | envelope invariants, taxonomy, metric capture, prompt fingerprint, persistence wiring | — |
| `test_outcome_store.py` | save→get_latest roundtrip, newest-wins, raw-output column, error rows | PostgreSQL |
| `test_latest_endpoint.py` | `/latest` serves from store (no run), cold miss, broken-store degrade, catch-all persist | — |
| `test_model_catalog.py` | staged model check (ingest + llm), endpoint split, soft-boot warnings | — |
| `test_model_governance.py` | pipeline-declared model (required), allowlist gate at assembly | — |
| `test_provider_factory.py` | `llm.provider` → implementation resolution; unknown name fails | — |
| `test_cost_recorder.py` | USD derivation, billing rows, latency column, session accumulators | PostgreSQL |
| `test_cost_report.py` / `test_perf_report.py` | section aggregation + pattern tables; fresh/legacy-DB guards | PostgreSQL |
| `test_stage_timer.py` / `test_run_footer.py` | shared timing capture + run-metrics footer | — |
| `test_embedder_cost.py` / `test_config_override.py` | embed cost wiring · base+user config deep-merge | — |
| `test_rag_live.py` 💸 | real embeddings end-to-end through store + retriever | `OPENAI_API_KEY` + PostgreSQL, `-m paid` |
| `test_llm_live.py` 💸 | one real structured LLM call (schema + usage) | `OPENAI_API_KEY`, `-m paid` |

## Continuous integration

`.github/workflows/tests.yml` runs the free suite on every pull request and on every
push to `master`, against a pgvector service container — the database-backed tests run
for real in CI. Paid tests never run in CI.
