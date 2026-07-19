# Testing

## Running

```bash
pytest tests/ -v        # free suite — no API cost, paid tests are excluded by default
pytest -m paid -v       # live tests against the real OpenAI API (fractions of a cent)
```

The default exclusion comes from `pytest.ini` (`addopts = -m "not paid"`). The
PostgreSQL-backed tests skip themselves when no database is reachable, so the free
suite is green everywhere.

## Database isolation

DB-backed tests never touch the operator's corpus. The `db_dsn` fixture (`tests/conftest.py`)
creates a throwaway `finiex_test` schema, applies the **real** `migrations/` into it, and returns
a DSN carrying `search_path=finiex_test,public` — isolation is the DSN's job, so no production
code knows a test is running. Tests therefore use the canonical table names (`articles`,
`cost_log`, …) against the exact schema the repo defines: a **broken** migration fails the suite
instead of hiding behind hand-written test DDL. `clean_db` adds a truncate for a per-test blank
slate. See [development/migrations.md](development/migrations.md).

Note what this deliberately cannot cover: the fixture builds its schema from scratch every run, so
**checksum drift never appears here** — it only exists against a database that already applied an
older version of a file. Drift is caught by `migrate_cli --status` and the boot guard, on a real
database, not by the suite.

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
| `test_rss_source.py` | RSS → Article mapping, idempotent ids, conditional GET (304), poll floor, typed HTTP/transport failures (429/5xx/retry) | — |
| `test_openai_embedder.py` | batching, order preservation, dimension guard (mocked client) | — |
| `test_pgvector_store.py` | idempotent upsert, recency/similarity query, importance filter | PostgreSQL |
| `test_retriever.py` | two-tier policy, top_k cap, near-dup collapse, tie-breaks (mocked) | — |
| `test_symbol_query_map.py` | constellation alias + base-currency fallback | — |
| `test_query_vector_cache.py` | cached query vectors, cache busting on config/model change | PostgreSQL |
| `test_ingestor.py` | fetch → skip known ids → embed only new → upsert; per-source counts; health record + quarantine skip; budget suspend | — |
| `test_coverage_report.py` | corpus coverage aggregation + console rendering | PostgreSQL |
| `test_prompt_builder.py` | Jinja2 `.md` fill + versioning; front-matter metadata + body hash | — |
| `test_pipeline_prompt_config.py` | pipeline-declared `prompt` block (name + version) | — |
| `test_sentiment_llm_output.py` | strict scored-subset schema (ranges, forbid extras) | — |
| `test_openai_provider.py` | structured call, error taxonomy mapping, cost capture (mocked) | — |
| `test_symbol_evaluator.py` | retrieve → prompt → LLM → enrich; provenance, raw-output capture | — |
| `test_pipeline_runner.py` | envelope invariants, taxonomy, guard degrade (ISSUE_35), metric capture, prompt fingerprint, persistence wiring | — |
| `test_output_guard.py` | coherence rules (signal↔score dead zone, HOLD confidence cap, empty reasoning, provenance backstop), knob overrides, basis skip | — |
| `test_outcome_store.py` | save→get_latest roundtrip, newest-wins, raw-output column, error rows | PostgreSQL |
| `test_corpus_guard.py` | corpus stamped with embedding model; mismatch refuses to boot | PostgreSQL |
| `test_source_set_registry.py` | source-set loading, duplicate ids, unknown reference, tracked configs | — |
| `test_workers.py` | interval-trigger loop, pass resilience, supervisor build (fan variants) | — |
| `test_breaking_detector.py` | cluster-tier boundaries + keyword fast-path (word-boundary), all LLM-free | — |
| `test_breaking_bus.py` | per-pipeline `min_importance` wake filter, re-arm, cross-set isolation | — |
| `test_event_trigger.py` | eval clock: immediate + interval + breaking wake before interval, clean stop | — |
| `test_breaking_report.py` | reaction math (engine vs end-to-end), episode grouping, funnel render | — |
| `test_source_health.py` | host normalization, warn/error split, report format, orphan notice, feed-doctor classifier | — |
| `test_source_health_store.py` | poll counters, flag+quarantine threshold, recovery reset, event cap, restart-survives quarantine | PostgreSQL |
| `test_logging_setup.py` | console + daily-rotating file, idempotent reconfigure, quiet loggers | — |
| `test_budget_guard.py` | cost circuit-breaker: quota-vs-rate-limit, suspend/cool-off/single-probe/resume, soft-daily warn, paid-seam reaction (fake clients) | — |
| `test_latest_endpoint.py` | `/latest` serves from store (no run), cold miss, broken-store degrade, catch-all persist | — |
| `test_model_catalog.py` | staged model check (ingest + llm), endpoint split, soft-boot warnings | — |
| `test_model_governance.py` | pipeline-declared model (required), allowlist gate at assembly | — |
| `test_provider_factory.py` | `llm.provider` → implementation resolution; unknown name fails | — |
| `test_cost_recorder.py` | USD derivation, billing rows, latency column, session accumulators | PostgreSQL |
| `test_cost_report.py` / `test_perf_report.py` | section aggregation + pattern tables; fresh/legacy-DB guards | PostgreSQL |
| `test_migration_runner.py` | ordered apply + record, re-run no-op, column added to a populated table, failed migration rolls back whole, checksum drift refuses, duplicate version, boot guard checks-but-never-applies, `-- finiex:no-transaction` (concurrent index needs it / builds with it / one statement only) | PostgreSQL |
| `test_stage_timer.py` / `test_run_footer.py` | shared timing capture + run-metrics footer | — |
| `test_embedder_cost.py` / `test_config_override.py` | embed cost wiring · base+user config deep-merge | — |
| `test_rag_live.py` 💸 | real embeddings end-to-end through store + retriever | `OPENAI_API_KEY` + PostgreSQL, `-m paid` |
| `test_llm_live.py` 💸 | one real structured LLM call (schema + usage) | `OPENAI_API_KEY`, `-m paid` |

## Continuous integration

`.github/workflows/tests.yml` runs the free suite on every pull request and on every
push to `master`, against a pgvector service container — the database-backed tests run
for real in CI. Paid tests never run in CI.
