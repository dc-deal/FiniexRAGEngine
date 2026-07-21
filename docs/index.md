# FiniexRAGEngine — Documentation

The engineering docs for the engine. Start with the architecture overview, then follow the
per-stage flow maps; the development docs cover running and operating it.

New here? Read in this order: **Architecture overview → the two flow maps → the stage docs
you need.**

## Architecture

- [Pipeline engine architecture](architecture/pipeline_engine_architecture.md) — how the
  engine is structured: the constellation model, the stage seams, the envelope contract.
- **Application flow** (the per-unit maps of each half of the run):
  - [01 · Ingest & retrieval](architecture/application_flow/01_ingest_and_retrieval.md) —
    fetch → embed → store → retrieve.
  - [02 · Analysis & outcome](architecture/application_flow/02_analysis_and_outcome.md) —
    prompt → LLM → guard → envelope → store.

### Stage & subsystem references

- [Retrieval policy](architecture/retrieval_policy.md) — the "squeeze": two-tier top-k,
  recency, dedup, the min-similarity floor and the retrieval funnel.
- [Prompt & LLM stage](architecture/prompt_and_llm_stage.md) — versioned prompts, structured
  output, model governance and served-snapshot capture.
- [Breaking detection](architecture/breaking_detection.md) — the LLM-free flag → priority
  eval → confirmed-push path.
- [Source health & logging](architecture/source_health_and_logging.md) — per-feed poll
  tracking, flag/quarantine, rotating file logs.
- [Weekly report & alerts](architecture/weekly_report_and_alerts.md) — the typed weekly
  model, the console + Telegram renderers, the scheduler and `/report`.
- [Output archive layout](architecture/output_archive_layout.md) — the rotated JSONL
  bucket naming shared with the collector (#9) and the Testing IDE (#141).

## Development & operations

- [Migrations](development/migrations.md) — versioned schema migrations; evolving a
  populated database without drop-and-recreate.
- [Config overrides (`user_configs/`)](development/user_configs_overrides.md) — the layered
  config truth, the load-path factories, and the startup override report.
- [Database inspection](development/database_inspection.md) — pgAdmin access and the
  diagnostic CLIs.
- [Testing](testing.md) — the suite layout, the `paid` marker, and what each test file covers.

## Elsewhere

- **[Vision & Roadmap](https://github.com/dc-deal/FiniexRAGEngine/issues/1)** (issue #1) —
  the full vision, the phased plan, and where the build stands.
- [`../README.md`](../README.md) — project overview, quickstart, feature status.
- [`../CLAUDE.md`](../CLAUDE.md) — the engineering rulebook (conventions + design decisions).
