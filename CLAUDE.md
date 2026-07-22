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

- **Plan first, build second — the two-eyes principle.** Implementation starts only after
  the operator has seen the plan. For an **issue-feature: always** — the operator may have
  read the issue days ago; the plan re-anchors it. Small-but-real changes get a short pitch
  in chat; anything bigger — and when in doubt — runs through **plan mode**. Only trivial
  few-liners skip the step. Explicit skip: only when the operator explicitly says to
  implement directly, without a plan. Explicit plan: when the operator asks for a plan,
  plan mode runs regardless of size.
- **A plan shows the target mechanics.** The anchor points (files/units to touch), the
  architecture and conversion steps, how the design carries planned future issues — and
  **exemplary target outputs** where they exist: a mock console output, a live-output line,
  a DB row, an envelope/JSON fragment. Every plan ends with an **architecture confidence
  in %**; below ~95% the plan asks **numbered questions** instead of guessing. Architecture
  decisions that surface *mid-build* come back to the table, never silently into the diff.
- **Look at the established systems.** Is there a comparable, mainstream system? Does it
  hit the same problem, and how does it solve it? Present the industry/established approach
  next to your own recommendation — not just an opinion.
- **Look at existing modules.** Check whether a well-established Python package already
  solves it well (or better) before hand-rolling. Adopt one only with a clear, lasting
  reason; when you do, update `requirements.txt` in the same change.

## Commit policy

- **Never create git commits.** The operator commits manually after reviewing each change.
- **Commit messages describe the change, not the tooling** — concise and imperative, no automated trailers.

## Versioning & releases

- **Scheme:** semver `MAJOR.MINOR.PATCH`; pre-1.0 tags carry an `-alpha` suffix
  (`v0.3.0-alpha`). The `version` string lives in `configs/app_config.json` and is mirrored
  by the `AppConfig` Pydantic default — the defaults-mirror test enforces they agree.
- **A version ships when its roadmap batch merges.** The operator tags the release (like
  closing issues — the assistant never tags, never runs `gh release`). Bump the `version`
  string in the same change that finishes the batch.
- **Release notes are the tag's description on GitHub** — the human-readable "what shipped"
  under the version tag (a one-paragraph framing + "Implemented & tested" + "Quality").
  `export_github_issues.sh` pulls every release's notes into
  `github_issues/release_notes/<tag>.md` — check there for orientation on what a past
  version delivered.
- **Roadmap #1** ticks a batch's checkbox only when it merges; the version's 🏷️ line is the
  batch's Definition of Done.

## Session start

Read first, in order:
- The roadmap — GitHub issue #1 (`FiniexRAGEngine — Vision & Roadmap`).
- The latest `HANDOFF_*.md` in the project root — current build state and next steps.
- `docs/architecture/pipeline_engine_architecture.md` — how the engine is structured.

## Code conventions

- **Fully typed — every signature, no exceptions.** Every parameter and every return carries an
  annotation: public and private, `__init__` and module-level helpers, sync and async. Specifically:
  - An optional collaborator is `x: Optional[Thing] = None` — **never a bare `x=None`**. The
    annotation costs an import; pay it. (This is where it drifted before: parameters appended to an
    existing signature by a later issue, where `=None` was one line and the annotation was two.)
  - A genuinely dynamic value (a DB cell, a serializer handler) is `Any`. An explicit `Any` is
    typed; an omission is not.
  - If a runtime import would cycle, use `if TYPE_CHECKING:` + a string annotation. Dropping the
    annotation is never the answer — and check first: `core/` never imports `api/`, so most feared
    cycles do not exist.
  - Verified mechanically (AST sweep over `finiexragengine/`), not by eye.
- **Domain modelling.** Runtime domain types → `@dataclass`; config schemas → Pydantic `BaseModel`
  (in `finiexragengine/types/config_types/`).
- **A shape that crosses a seam lives in `types/`.** If another module must import it to write a
  signature, it is a domain type → `types/<domain>_types.py`, grouped by domain
  (`ingest_types`, `eval_types`, `article_types`, …). A shape built *and* consumed inside one
  module (a report's row/section) stays with it — do not scatter a self-contained unit.
  **`types/` never imports from `core/`** (checked: it does not today) — so when a shape moves,
  everything it references moves with it or the move is wrong.
- **Stage boundaries return result objects, never bare collections.** A seam another layer
  calls returns a typed result `@dataclass` (`RetrievedContext`), not a bare `List[…]` or a
  tuple — a result object extends additively, a bare return refactors every call site (the
  funnel build's one expensive step was exactly this conversion). When an existing bare
  return needs a second value, refactor it into a result object then — never bolt on a tuple.
- **Group by domain, never by mechanism.** Every `core/` directory names a domain (`sources`,
  `rag`, `llm`, `pipeline`, `outcome`, `observability`, `triggers`, `ui`) — never a technique. "It
  touches psycopg", "it is a store", "it is a report" is not a domain: a unit lives with its
  consumers and its lifecycle. So `pgvector_store` stays in `rag/` (meaningless without the
  retriever/embedder) and `source_health_store` in `observability/` (meaningless without its
  report) — collecting them into a `store/` folder would group eleven unrelated files whose only
  bond is a driver, and would flatten the deliberate *"two stores, distinct roles"* split below.
  Sub-folders group by domain too (`observability/reports/`), and only once a directory is
  genuinely crowded — a prefix (`ingest_*`, `eval_*`) already groups an alphabetical listing for
  free, at zero import churn. `ui/` is the live operator console (`EngineStats` live state +
  `LiveDisplay` renderer, ISSUE_26) — a domain distinct from `observability/reports/`'s
  store-backed batch surfaces (live in-memory vs read/aggregate over the store), not an "it
  renders" mechanism bucket.
- **A file's name says what it *is*.** `openai_errors.py` holding no exception (only a
  classifier) is a naming bug, not a placement one — it invited "move it to `exceptions/`",
  which would have leaked one vendor's vocabulary into a shared leaf. Rename before relocating.
- **String literals use single quotes**; double quotes only for f-strings and docstrings.
- **Imports at the top**, grouped standard library → third party → project. Never mid-file.
- **No `__init__.py`** — fully-qualified imports from the package root `finiexragengine.`.
- **One *behaviour* class per file**; file name = class name in snake_case. ABCs in their own
  `abstract_*.py` file, named `Abstract<Concept>`. Data shapes are not "classes" for this rule —
  they group by domain (`types/*_types.py` hold many). Module-level functions are fine when they
  are file-private helpers (`_fmt`) or a deliberate function module (`provider_factory`,
  `envelope_contract`) — but a **public** function that other layers import is its own unit:
  if the API and the CLI both reach into an engine file for it, it is in the wrong file.
- **Private members** carry a `_` prefix; expose via getters/setters. No external `obj._x` access.
- **All datetimes timezone-aware UTC.** The analysis timestamp is real-time wall-clock (this
  is a live service); consumers stamp their own collection time downstream.
- **Custom exceptions** in `finiexragengine/exceptions/` (`*_errors.py`), rooted at `FiniexRagError`.
- **Config managers** in `finiexragengine/configuration/`; instantiate and use directly.
  Config defaults must mirror the JSON config file exactly.
- **Config truth is layered — and the factories are the only load paths.** Tracked `configs/`
  carries the shared defaults; a gitignored `user_configs/` overlay (`app_config.json`,
  `pipelines/*.json`, `source_sets/*.json`) deep-merges on top at load — secrets,
  machine-specific switches, local experiments. Registries load **only** via
  `AppConfigManager.build_pipeline_registry()` / `build_source_set_registry()` (raw
  constructors are test-only) — a call site assembling its own registry silently drops
  the override layer. Every applied override reports at startup, leaf by leaf
  (`[OVERRIDE] …`, gated by `logging.warn_on_override`).
  Details: `docs/development/user_configs_overrides.md`.
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

## LLM stage principles

- **Design against the provider seam.** Every LLM-stage feature is designed against
  `AbstractLLMProvider`, never against OpenAI specifics — provider swappability
  (OpenAI ↔ fine-tune ↔ self-hosted OpenAI-compatible ↔ future providers) is a standing
  review question for any change touching the LLM stage. Provider-specific behavior stays
  inside the concrete provider; `llm.provider` selects the implementation via
  `provider_factory` (a new entry = a genuinely different API protocol).
- **The eval model is series-defining, like the prompt.** Pipeline-declared (required, no
  global default), gated by `llm.allowed_models`, and the served snapshot (`response.model`)
  is recorded per call and per envelope (#40).

## Observability & cost (capture at the call, report from the store)

- **Metrics are a byproduct of the run.** Token usage, cost, and per-stage/per-call latency are
  written into `RunMetadata` and persisted with the envelope (the outcome store is the metrics
  warehouse); reporting is a read/aggregate over it, not a separate telemetry system.
- **Every stage is tracked — cost and performance.** Any new stage or paid call wires in the
  shared units: `StageTimer` for stage durations, `CostRecorder` for tokens/USD *and*
  `duration_ms` (one row per API call = one latency sample, traceable via
  ts/section/model/pipeline_id). Not optional per feature — part of a stage's Definition of Done.
- **Reports share the pattern table.** Every metrics surface (cost, performance, coverage — and
  future ones) renders the same console pattern: title + window line + `----` dividers + aligned
  columns; spending CLI passes end with the `--- run metrics ---` footer (`RunFooter`).
  They live together in `core/observability/reports/` (`build_*` + `format_*` + their own row
  shapes per file — a self-contained unit). The shared primitives they render *with*
  (`RunFooter`, `StageTimer`) stay one level up: `StageTimer` is used by the engine itself, so
  the ingestor must never import from `reports/`.
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
  core/                 the pipeline engine — one directory per domain, never per mechanism
    sources/  triggers/  rag/  llm/  pipeline/  outcome/
    observability/      metrics units (recorder, guard, timer, footer, logging)
      reports/          the console surfaces (build_* + format_*)
    ui/                 the live operator console — EngineStats (live state) + LiveDisplay (rich renderer)
  exceptions/           custom errors
  types/                @dataclass domain types + config_types/ (Pydantic)
  utils/                dependency-free helpers (pure functions, no engine imports)
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
- **Comments vs body:** additions to a **not-yet-begun** issue always go into the **body**
  (the body stays the spec). Once implementation has started, progress, deviations and
  decisions land as dated **implementation-notes comments** — effectively: comments only on
  issues whose build has begun. The snapshot export includes comment threads; re-run it
  before sessions that need fresh issue context.
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
- **Stage-scoped reads.** Before working on a pipeline stage, read the matching
  `docs/architecture/application_flow/` map first — `01_ingest_and_retrieval.md` (ingest + retrieval)
  or `02_analysis_and_outcome.md` (LLM analysis + outcome) — the per-unit maps of each flow.
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
