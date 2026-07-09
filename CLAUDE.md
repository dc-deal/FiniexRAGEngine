# CLAUDE.md — FiniexRAGEngine Project Rules

**The project's engineering rulebook** — shared conventions and design decisions for
FiniexRAGEngine, including how the codebase is built with an AI assistant. Public, so the
workflow is transparent.

**Living document.** These rules grow as the build proceeds — when a convention, contract, or
design decision is agreed during a session, record it here in the same change so it survives
into the next session. Only codify what is actually decided; leave still-open recommendations out.

**Changes to this file need sign-off.** Never edit CLAUDE.md unilaterally — present the proposed
change to the operator and get explicit confirmation first; only then apply it.

## AI-assisted development

This codebase is built pair-programming with an AI assistant (Claude Code, Anthropic
Opus/Fable). The tooling is openly acknowledged — nothing here is ghost-written and hidden;
the product's own LLM usage (OpenAI API) is a core feature, described openly.

Discipline: the assistant proposes and drafts; the human owns architecture and review;
every change is committed manually after review.

## Working style

- **State confidence, ask when low.** Communicate implementation confidence as a
  percentage; when it is below ~95%, or a change is public-facing / hard to reverse,
  ask focused, numbered questions before executing instead of guessing.
- **Addressing.** The human is "the operator"; German (informal *du*) is fine in chat.
  All artifacts — code, comments, docs, issues, commit messages — stay English.

## Architecture planning

Before committing to a design for a non-trivial feature or change:

- **Look at the established systems.** Is there a comparable, mainstream system? Does it
  hit the same problem, and how does it solve it? Present the industry/established approach
  next to your own recommendation — not just an opinion.
- **Look at existing modules.** Check whether a well-established Python package already
  solves it well (or better) before hand-rolling. Adopt one only with a clear, lasting
  reason; when you do, update `requirements.txt` in the same change.

## Commit policy

- **Never create git commits.** The operator commits manually after reviewing each change.
- **Commit messages describe the change, not the tooling** — concise and imperative, no automated trailers.

## Session start

Read first, in order:
- The roadmap — GitHub issue #1 (`FiniexRAGEngine — Vision & Roadmap`).
- The latest `HANDOFF_*.md` in the project root — current build state and next steps.
- `docs/architecture/pipeline_engine_architecture.md` — how the engine is structured.

## Code conventions

- **Fully typed.** Runtime domain types → `@dataclass`; config schemas → Pydantic `BaseModel`
  (in `finiexragengine/types/config_types/`).
- **String literals use single quotes**; double quotes only for f-strings and docstrings.
- **Imports at the top**, grouped standard library → third party → project. Never mid-file.
- **No `__init__.py`** — fully-qualified imports from the package root `finiexragengine.`.
- **One class per file**; file name = class name in snake_case. ABCs in their own
  `abstract_*.py` file, named `Abstract<Concept>`.
- **Private members** carry a `_` prefix; expose via getters/setters. No external `obj._x` access.
- **All datetimes timezone-aware UTC.** The analysis timestamp is real-time wall-clock (this
  is a live service); consumers stamp their own collection time downstream.
- **Custom exceptions** in `finiexragengine/exceptions/` (`*_errors.py`), rooted at `FiniexRagError`.
- **Config managers** in `finiexragengine/configuration/`; instantiate and use directly.
  Config defaults must mirror the JSON config file exactly.
- **CLI entry points** in `finiexragengine/cli/` — parameter reception only, no logic.
- Early-exit pattern preferred. Keep diffs minimal; no changelog/version comments in code.
- **Comment the flow generously as you build.** Comment each meaningful step —
  when in doubt, one comment too many beats one too few — giving the mechanics and
  the *why*, so the operator can follow what was built without re-deriving it.
  (Applies to explanatory comments; functional diffs still stay minimal.)
  Public-repo standard: English, compact, professional — no session/tooling
  references, no narration, no changelog/version notes; trace a step to its issue
  with `ISSUE_N` where relevant.

## Engine output contract (envelope invariants)

Every run returns a valid `AnalysisEnvelope` JSON — a downstream collector must be able to parse
every response, success or failure.

- **Every requested symbol is always present** in `result`. No data for a symbol →
  `signal: 'HOLD'`, `confidence: 0.0`, `reasoning: 'No relevant news found'`, `sources: []`.
  A missing symbol is a bug, never "no signal".
- **Prefer `status: 'partial'` over `'error'`.** If some sources fail but data remains, analyse
  what is there and record the degradation via `metadata.sources_reached`. Reserve
  `status: 'error'` (empty `result`) for when nothing could be produced.
- **Always return a parseable envelope, even on internal failure** — the API catches engine
  errors and returns `200` with `status: 'error'` and populated `errors`, never a bare `500`.
- **`RunError.type` is from a fixed taxonomy**, not a free string: `SOURCE_UNREACHABLE`,
  `SOURCE_PARSE_ERROR`, `LLM_TIMEOUT`, `LLM_API_ERROR`, `LLM_PARSE_ERROR`, `VECTOR_STORE_ERROR`,
  `PARTIAL_RESPONSE` — each maps to a `FiniexRagError` subclass.
- **Bump `prompt_version` whenever the internal prompt changes** — different prompts yield
  different scores for the same news; the consumer must keep the series apart (replay/backfill).

## Ingest & retrieval principles

- **Store the full raw corpus; never discard at ingest.** Acquisition fetches → embeds →
  upserts *every* article (idempotent). Relevance is contextual and per-query, so it is a
  retrieval-time decision, not an ingest-time one. Discarding at ingest would break
  replay/backfill and cross-pipeline corpus reuse.
- **Token/relevance filtering happens at retrieval, not at storage.** The cheap filter is the
  embedding + vector similarity (no LLM); `top_k` is the hard token cap. Recency dominates for
  current-mood signals; older items enter only when an importance tier asks for them.
- **Breaking detection is cheap, not per-article LLM.** Cluster-burst / source-weight / keyword
  heuristics in the ingest worker flag a *candidate*; reserve the LLM for the candidate and the
  actual evaluation. Stage 1 flags, the evaluation confirms before pushing.
- **Two stores, distinct roles.** Article corpus = pgvector (raw text + vector + metadata +
  importance tag), shared across pipelines. Outcome store = produced envelopes (source of
  truth) served via `/latest` and archived downstream as JSONL + collection time.
- **RAG belongs on unstructured text only** (news, blogs, social, filings/statements).
  Structured/numeric data (prices, on-chain, order flow) does **not** go through embed/retrieve
  — use an `API` source that emits structured facts, or SQL. Litmus for a new pipeline: is the
  primary input unstructured text the LLM must read and distill?

## Observability & cost (capture at the call, report from the store)

- **Metrics are a byproduct of the run.** Token usage, cost, and per-stage/per-call latency are
  written into `RunMetadata` and persisted with the envelope (the outcome store is the metrics
  warehouse); reporting is a read/aggregate over it, not a separate telemetry system.
- **Capture token usage at the call** (OpenAI `usage`) — it is irreconstructable afterwards.
  Cost is derived from a per-model price table in `app_config.json` (reproducible, like `prompt_version`).
- **Track spend, not balance.** The remaining account balance is not reliably exposed via API;
  accumulate spend and compare to a configured budget.
- **A run that spends budget reports the spend in its own output.** Any CLI or pass that makes
  paid calls (embeddings, LLM) surfaces the count where it runs — e.g. `embedded N (paid)` — so a
  cost is never silent. The persisted-envelope metrics stay the durable warehouse; this is the
  at-the-call echo.
- **Structured, levelled logging** (per `log_level`); every `RunError` is logged with its
  taxonomy type. Error statistics are aggregated from the persisted envelopes' `status`/`errors`,
  not parsed from log text.

## Project layout

```
finiexragengine/        package root (no __init__.py)
  api/                  FastAPI app + endpoint routers
  cli/                  CLI entry points
  configuration/        config managers
  core/                 the pipeline engine
    sources/  triggers/  rag/  llm/  pipeline/  store/
  exceptions/           custom errors
  types/                @dataclass domain types + config_types/ (Pydantic)
configs/                app_config.json + pipelines/*.json (constellations)
docs/                   architecture + guides
tests/                  pytest suite
```

## Testing

- Run the full suite: `pytest tests/ -v`. Report real pass/fail counts honestly.
- Plain pytest + markers only — no custom test runner (transparency; the project is small).
- Tests that spend API budget carry the `paid` marker (`*_live.py` files); default runs and
  CI exclude them via pytest.ini. Run deliberately: `pytest -m paid -v`.
- New behavior gets tests. New test suites get a doc note (`docs/testing.md`).

## Issues

- `ISSUE_*.md` in the project root are drafts for transfer to the issue tracker (gitignored).
  Cross-reference related issues with a `**Related:**` line near the top.
- **Draft → operator review → upload on OK.** New issue drafts land as `ISSUE_*.md` in the
  project root; the operator reads them first. Only on explicit OK does the assistant create
  them on GitHub (`gh issue create`, one at a time) — never push an issue to the tracker
  unprompted. (The bulk re-import script is retired; issues are added individually now.)
- Mention test + docs follow-ups at the bottom of an issue where relevant.
- **List issues as a checklist, never a table:** `- [ ]` / `- [x]` + `#N` + a short description
  — **not** the title (GitHub renders the title from the `#N` reference). The checkbox carries the
  done state; do not add a separate "done"/status column or word.
- **Never close/resolve issues.** The operator closes them at merge via `resolves #…`. The
  assistant may tick the roadmap checkbox (`[x]`) to show progress, but must never run
  `gh issue close` (or otherwise resolve an issue) — ticked ≠ closed; the issue stays open until merge.
- Root-level gitignored working files (`ISSUE_*.md` drafts, `INTERNAL_*.md`) are the operator's
  scratch space; the **operator prunes them once processed** (by processing status). A missing
  one means "done / transferred", not data loss — GitHub is the durable copy for issues. Do not
  re-create a pruned file unless asked.

## Documentation

- Docs in `docs/`. New structures/features get documented; review `README.md` per change.
- **Stage-scoped reads.** Before working on the ingest or retrieval stage, read
  `docs/architecture/detailed_ingest_and_retrieval.md` first — it is the per-unit map of that flow.
- English everywhere. Human-readable, compact.

## After each feature (five-point review)

"Code done" is not "done". When a feature or fix is finished, walk these five and state
what each needs (the operator decides and applies):

1. **Tests** — new behavior gets tests; changed behavior updates them.
2. **Docs** — always review; new structures/features get documented, touched flows get
   their doc updated.
3. **README** — check whether the change touches it (status, quickstart, feature list).
4. **Issues** — if the work came from an issue, fold implementation decisions/deviations
   back into it (render an updated `ISSUE_<name>.md` for the operator to sync).
5. **Roadmap** — keep issue #1 current; tick a box only when the item ships (merges).
