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
  All artifacts — code, comments, docs, issues, commit messages, handover documents — stay
  English. **The language of the conversation never sets the language of a file**, and a doc
  written *for* the operator, about their own workflow, is still an artifact: a German chat about
  a runbook produces an English runbook. That is where it slipped once.
  **Gitignored is not an exemption.** `ISSUE_*.md`, `INTERNAL_*.md` and `HANDOFF_*.md` are
  artifacts too — private, not exempt. Neither a file's audience nor its visibility changes its
  language; only chat is German.

## Architecture planning

Before committing to a design for a non-trivial feature or change:

- **Check /app/.vscode/launch.json**
  As a central launch library, it provides a quick overview of the project's most important capabilities from the operator's perspective. Every command listed here has its own purpose and justification. If something is not included, it may have been intentionally omitted for clarity and overview reasons. The `launch.json` is structured into sections. Make sure to extend the appropriate sections when adding new entries, and always consider the section headings and comments as guidance.
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
  by the `AppConfig` Pydantic default — the defaults-mirror test enforces they agree. A third
  copy sits in the README status line (`> **Status:** Alpha · v0.3.2-alpha …`) and **no test
  guards it** — bump all three in the same change.
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

## Two environments — and you only ever see one of them fully

**The assistant works in the local dev container. The live engine runs elsewhere and is reachable
only as a read-only HTTP surface — never its database, never a shell.** That is the assumption
most likely to produce a confident wrong answer, because the dev container holds a database with
the same schema, the same table names and a handful of real-looking envelopes — a query against it
returns a plausible number for a question that was about production.

| | dev container (what you see) | live server (where the engine runs) |
|---|---|---|
| Host | Linux container on the operator's laptop | Windows Server, reached by RDP |
| `outcomes` journal | a few hundred envelopes from test runs | the real series, weeks of continuous operation |
| RAM | the laptop's | **16 GB** |
| Disk | hundreds of GB free | **~149 GB total, and treated as scarce** |
| Reachable from here | yes, directly | **read-only over HTTPS** (`/v1/*` with a token; `/health` + `/build` public) — no database, no shell |

**Since 2026-08-24 there is one exception, and it is narrow.** The live engine has a public TLS edge
and per-consumer tokens (ISSUE_98), and the assistant holds its own (`claude-dev`, revocable without
touching the Testing IDE's). So a handful of production questions are now answerable *from
production*: is the engine alive, what do its workers report, what did the last pass actually
produce, what does a served envelope contain field by field. Those may be answered directly.

**Everything else is unchanged, and the two consequences below still govern.** PostgreSQL is not
exposed and will not be: every aggregate, every historical count, every "how often since X" is still
a question the dev journal answers with real, small, irrelevant numbers. Reaching for the API does
not make a SQL question answerable — it makes exactly the read-only HTTP surface answerable.

**The environments are now distinguishable mechanically**, which is the part worth keeping:
`/v1/health` reports `journal_id` — production `138c68e48b15`, the dev container `9c3fa4c80d95` —
and `/v1/build` reports the commit the live process actually imported. Where a number's origin used
to be an assumption, it can be checked.

The engine cannot be made to spend money this way: `POST /run` is not registered in production, so
every route the assistant can reach is a read.

**A developer with a live engine of their own** puts their client-side bearer token in `.env` under
`FINIEX_LIVE_CLIENT_TOKEN` — deliberately not `FINIEX_API_TOKENS`, which is the server side (the
tokens an engine *accepts*). The two are one character apart and mean opposite things. `.env` is
gitignored; `.env.example` carries the key with an empty value.

Two consequences, both learned the hard way:

- **Never answer a question about production from the dev journal.** "Does the journal predate
  2026-07-22?" is a question about the server; the dev database answers it with numbers that are
  real, small and irrelevant. Say the query has to run on the server, and give the query.
- **Anything sized against dev resources has to be re-checked against the server.** A generator run
  peaking at 1.5 GB RSS and half a gigabyte of output is unremarkable on the laptop and a
  significant fraction of the server. The operator moves artifacts to the server deliberately; a
  process or a file that only fits here is not finished.

Everything the HTTP surface does not cover, the operator still bridges by hand (RDP, file copy,
`export_cli` and any SQL run on the server). There is no tunnel and no exposed database, and asking
for either has costs the operator has already weighed — the edge that exists was built deliberately,
route by route, and is not an opening to widen casually.

## The container is disposable — its home is not

**Never recommend or confirm a container rebuild without first checking the transcript backup, and
say what you found.** `~/.claude/projects/` holds the session transcripts, and it lives inside the
container: a rebuild deletes whatever is not on a volume or a bind mount — silently, irreversibly,
and with no prompt.

Two tiers exist here and only the first is automatic. `devhome:/root` is a named volume: it survives
every rebuild, and dies with `docker compose down -v`. `.devcontainer/local/home-seed`, written by
`backup_home.sh` and restored by `restore_home.sh` on container create, survives volume deletion —
but is only ever as fresh as its last manual run.

**This is a rule for the assistant, not documentation for the operator**, and that distinction is
the whole point: nobody opens a rulebook at the moment they click rebuild, and a pre-rebuild
instruction living in a script header is read by whoever is already looking at the script. The
assistant is in the conversation when a rebuild comes up — usually because it proposed one — so that
is where the check belongs. Report the seed's age and transcript count *before* the rebuild runs,
not after.

And prefer the mechanical fix to the reminder: a bind mount of `projects/` needs nothing remembered,
which is the difference between a backup and an intention.

## The cross-project bus is operator-initiated

**Never read or write the bus unless the operator asks for it.** A shared folder mounted at `/bus`
carries questions and answers between this project and its siblings, through `bus_*` tools from a
client that lives on the bus itself. Its inbox is not something to check on your own initiative —
not at session start, not in passing while looking for something else, and not because a question
at hand happens to suit it.

The reason is not tidiness. A message written to the bus lands in another project's inbox and is
read by whoever works there, so sending one is an outward-facing act; reading one pulls another
project's material into this conversation. Neither is a step taken to be helpful.

**There is no mechanical gate, and that is a decision rather than an omission.** Two were built,
both worked, and both were removed. A later session must not read their absence as an oversight and
rebuild them, so the reasons are recorded here.

A `permissions.ask` rule on the bus tool names fires correctly and costs nothing. It was removed
anyway: the policy is that the rule above governs bus access by itself, and a prompt standing behind
it invites the discipline to be delegated to a dialog box. The discipline is the whole protection.

A PreToolUse hook covering the shell route also works — a `cat` on a message file raises a prompt
that a tool-name rule cannot reach. It was removed because **a tool carrying a matching PreToolUse
entry stops being auto-accepted**: a catch-all matcher does not add friction to bus access, it ends
auto mode for every tool in every session, and it arrives disguised as "suddenly everything asks"
rather than as a bus problem. Answering `permissionDecision: "allow"` for non-matching calls would
restore auto mode and is worse — a hook that answers "allow" overrides the operator's other
permission rules, including denials. A protection that suspends a larger protection is not one.

There is likewise no SessionStart hook: one that prints the inbox at every start is precisely the
unbidden read this rule forbids.

So nothing stands between the assistant and that folder except the rule. Concretely: no unprompted
read — not `bus_inbox`, not `bus_threads`, not a `cat` on a message file; no check "while I am
here"; no poll because something might have arrived; no look at the start of a session or a task.
No write without an explicit request. The failure mode to design against is a **read**, and it has
already happened once.

`bus_inbox` is never on its own evidence that nothing arrived — a `note` enters no inbox by design,
which produced three false all-clears in one hour. Pair it with `bus_threads` and compare the count;
report "nothing" only when both are empty. Client 1.1.0 appends that count itself, but only once a
session has restarted: the stdio server imports the client at spawn and does not reload it.

Adding `"disabledMcpjsonServers": ["finiex-bus"]` removes the tools from context altogether.

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
  - Verified mechanically by `tests/contracts/test_typing_contract.py`, not by eye — so the rule runs on
    every suite and in CI instead of being remembered. It checks two different things: that every
    annotation **exists** (AST sweep over `finiexragengine/`), and that every annotation
    **resolves** (`typing.get_type_hints`). The second half is not redundant: since Python 3.14
    (PEP 649) annotations are evaluated lazily, so a name that was never imported no longer fails
    at import time — `Callable` sat undefined in a live signature for weeks, with a green suite.
    Names bound under `if TYPE_CHECKING:` are honoured as resolvable, so the sanctioned pattern
    above is never flagged.
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
- **Names are searchable; specific beats short.** A term another file greps for has to be
  *findable*: a few characters more, and the search returns the thing you meant instead of
  everything. `Signal` in a trading codebase already means signal data, a signal series and the
  consumer's SIGNAL worker — `SentimentSignal` costs nine characters and is unambiguous. `basis`
  collides with a cost basis and a hash basis in this repo, which is why the vocabulary alias is
  `ResultBasis`. Applies to type aliases, constants, public functions and domain terms in docs;
  a file-private helper (`_fmt`) may stay short, because nobody searches for it.
- **Closed vocabularies are strict at the producing seam, permissive at the parsing boundary.**
  The value domain lives as a `Literal` alias plus a data tuple (`ResultBasis` / `RESULT_BASES`)
  and types the code that *builds* a row, so a typo fails where it is written. The model field
  itself is a plain `str`: an archived envelope carrying a value a later version introduced must
  still load, because the envelope contract's "always parseable" rule outranks type strictness.
  Currently: `TriggerReason`, `RunError.type`, `signal`, `basis`, `status`, `data_origin`.
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
- **One report, one command, one route.** A parameter must never decide *which* report you get.
  If it is its own report — its own question, its own shape — it gets its own CLI entry point and
  its own address under `/v1/reports/<name>`. A flag may **narrow** a report (window, symbol,
  source id); it may not **replace** it. A fixed composite is fine and is something else entirely:
  `sources_cli` prints health next to latency because the operator's question spans both, and no
  flag selects between them. What is not fine is `--timeline` turning a funnel report into a series
  report — a second program wearing the first one's name, invisible until you read the flags.
  ISSUE_104 split five such modes out; the API had already been forced into the right shape,
  because an address has to name one thing.
- **Access is granted by name, never by omission.** Every consumer token declares `grants` — a
  list of `<surface>:<name>` (`reports:source_health`, `pipelines:crypto_sentiment`), with
  `<surface>:*` and a bare `*` — and the field is **mandatory**: a token without it fails at boot
  instead of defaulting to everything. So a surface added later is unreachable by a consumer until
  someone writes its name into their token. Granting is an act; it is never inherited from a
  default nobody chose.
  - **A grant names a thing, not a route.** `reports:source_health` keeps meaning what it means
    across a rename or a `/v2`; a path-shaped rule would silently stop matching and answer a
    consumer who did nothing wrong with a 403. Comparison is exact — no wildcard matching against
    caller-supplied paths, which is where authorization defects live.
  - **Bound to the route by FastAPI's own mechanism.** The *surface* is declared once per domain
    router (`Security(dependency, scopes=['reports'])` — `SecurityScopes`), the *name* is the
    route's first path parameter. A collection route has no identity segment and is therefore
    filtered in its handler rather than gated, so a caller entitled to some of what it lists still
    gets an answer.
  - **Know the one weakness: this half is NOT inherited.** Authentication sits on the single shared
    protected router, so every route inherits it and nobody can forget it. Authorization cannot work
    that way — the surface is per-router information — so a **new domain router that omits
    `Security(..., scopes=[...])` would be authenticated but ungated**, reachable by any valid
    token. That is the failure mode to watch when adding a router, and it is the reason
    `tests/api/test_report_scopes.py` walks every registered identity route and asserts a token holding
    nothing is refused: the declaration is not trusted, it is checked. **A new router means a new
    surface in `GRANT_SURFACES` and a `Security` declaration — or the suite says so.**
  - **`active: false` is a kill switch**, not documentation: a consumer can be switched off without
    deleting their token, and an inactive entry never enters the registry.
  - Each token carries a `note` saying who holds it, and the boot log prints both
    (`[AUTH] token ide · grants: reports:source_health, pipelines:* · Testing IDE`) plus any
    inactive entry — a grant that lives only in a config file is a grant nobody checks.
  - The environment form (`FINIEX_API_TOKENS="name:token"`) has nowhere to put grants and therefore
    means `*`; it exists for a container or CI, which are ours, never for a consumer.
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
  **Versions only move forward.** A prompt is never edited in place and never reverted — a
  correction is the next version, and the superseded file stays in `prompts/<name>/` as the record
  of what produced the archived series. A version that turned out wrong keeps its number and gains
  a note saying so, in its own front matter and in the issue that supersedes it. Deleting or
  rewriting a version orphans every envelope carrying it.
  **And a bump reports its effect, not merely its existence.** `prompt_version` says which prompt
  ran; it cannot say the new one answers differently. v2→v3 (2026-08-23) cut the crypto confirm rate
  8.43% → 0.47% because a display-field instruction quietly added a qualification test to breaking,
  and nothing compared the distributions for three days (#110). A prompt change records its
  before/after score distribution in the issue that makes it.

## Ingest & retrieval principles

- **The ingest pass is three phases, and only the middle one is concurrent.** `plan` (who is
  polled at all — reads the shared quarantine state and hands out the half-open probe), `fetch`
  (pooled per `SourceSetConfig.fetch_workers`), `account` (health, journal, embed, upsert,
  detection — everything that costs money or mutates state, in declared order). `fetch_workers`
  therefore changes *when* feeds are pulled and never what a pass concluded; a test asserts the
  pooled and sequential result objects are identical. Widening that boundary is an architecture
  decision, not a tuning one — the paid stages are sequential on purpose, and the budget-suspend
  path depends on it.
- **`SourceHealthStore` is safe under concurrent per-source calls — settled, not an open
  question.** It has been re-derived in more than one session, so the answer lives here. The
  reasons are structural, not luck: `_connect()` opens a psycopg connection **per call** (no
  shared cursor); `_PassState` accumulates into `Set[str]` rather than integer counters (atomic
  under the GIL *and* idempotent per `source_id`, which is what keeps the correlated-failure
  denominator honest); the policy decision is deferred to `_resolve_pass`, which runs
  single-threaded after `pass_scope` closes; and one thread only ever touches one `source_id`.
  Verified against Postgres on cloned tables, 2026-08-25 — 25 rounds × 12 concurrent recorders
  with no accumulator mismatch, and both policy paths (correlated-failure guard, flag ladder)
  behaving as designed. Method and numbers:
  `docs/architecture/application_flow/01_ingest_and_retrieval.md`. Do not re-derive it — extend
  that record if the shape changes.
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
tests/                  pytest suite — one folder per domain, mirroring the package
```

## Testing

- Run the full suite: `pytest tests/ -v`. Report real pass/fail counts honestly.
- Plain pytest + markers only — no custom test runner (transparency; the project is small).
- Tests that spend API budget carry the `paid` marker (`*_live.py` files); default runs and
  CI exclude them via pytest.ini. Run deliberately: `pytest -m paid -v`.
- **The suite mirrors the package — a test lives where its subject lives.** `tests/rag/` covers
  `core/rag/`, `tests/api/` covers `api/`, `tests/observability/reports/` covers
  `core/observability/reports/`: finding a unit's tests is the same navigation as finding the
  unit. A new test goes into the folder its subject already occupies; **if none fits, create the
  folder for that category** rather than dropping the file at the root — a flat root of 91 files
  is what the 2026-08-26 split replaced. Two folders are deliberately not mirrors: `contracts/`
  holds the guards that are about the *codebase* rather than a unit (the typing sweep, the
  closed-vocabulary boundary, the layout guard itself), and `generator/` holds the tests for the
  sample generators under `experiments/`. Sample **data** files go to `tests/fixtures/<domain>/`;
  a factory helper that builds a shape per case stays with its test — a static file cannot vary
  per case, which is why nothing was outsourced in the split.
  No `__init__.py` anywhere, so pytest imports each module by its bare basename: **basenames stay
  unique across the whole tree** — two `test_report.py` in different folders collide at
  collection. Checked by `tests/contracts/test_suite_layout.py`, which also refuses a new file at
  the root: the failure mode of a move is a test that is no longer *collected*, which is a green
  suite with less coverage.
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
