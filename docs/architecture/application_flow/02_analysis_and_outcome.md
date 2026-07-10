# Detailed Analysis Stage & Outcome

Picks up where `01_ingest_and_retrieval.md` leaves off. Retrieval has handed a small,
on-topic `List[Article]` **per symbol**; here that context becomes a typed signal, and the
per-symbol signals are assembled into the outcome envelope that leaves the engine.

**Status:** Phase C (analysis) is **built** (ISSUE_6), and Phase D's assembly is **built**
(ISSUE_7 — `PipelineRunner`): `POST /run` executes the real staged flow when a database is wired
(`create_app` attaches runners; without `DATABASE_URL` the scaffold mock still answers). Persist →
serve remains ISSUE_8 → ISSUE_9. Built steps name real code; planned steps name the unit they
will live in.

Companion docs: `01_ingest_and_retrieval.md` (the write + read paths) and
`../prompt_and_llm_stage.md` (the LLM stage in depth).

## Phase C — Analysis (per symbol, built · ISSUE_6)

Top-down, one symbol's retrieved context flows through:

1. **Context in — `core/rag/retriever.py` (`Retriever.retrieve`).**
   The output of Phase B: at most `top_k` distinct, recent, on-topic `Article`s for this symbol
   (the comparison scores were already dropped — only the selected articles travel on).

2. **Build the prompt — `core/llm/prompt_builder.py` (`PromptBuilder.build`).**
   Renders the versioned Jinja2 Markdown template `prompts/<name>_v<prompt_version>.md`: fills
   `{{ symbol }}` and loops the retrieved articles (`{% for a in articles %}`) into a numbered list
   with source + timestamp. Wording **and** formatting live in the reviewable template, out of code;
   `prompt_version` pins the exact file, so the same version always yields the same prompt
   (replay/backfill).

3. **Structured LLM call — `core/llm/openai_provider.py` (`OpenAIProvider.complete_structured`).**
   Chat-completions with a `response_format` JSON schema, low `temperature` + `timeout` from
   `LlmConfig`. Returns an `LlmCompletion(data, usage)`. Failures map to the taxonomy — timeout →
   `LLMTimeoutError`, backend → `LLMApiError`, non-JSON → `LLMParseError` (all `LLMError`). Token
   `usage` is captured at the call (irreconstructable afterwards).

4. **Validate the scored fields — `types/outcome_types.py` (`SentimentLlmOutput`).**
   The parsed JSON is validated into the strict scored subset — `signal`, `sentiment_score`,
   `confidence`, `reasoning`, `urgency` (extra fields forbidden). The LLM scored **only the mood**; a
   malformed completion is rejected here (`LLM_PARSE_ERROR`).

5. **Cost — `core/observability/cost_recorder.py` (`CostRecorder`).**
   The call's `usage` is priced from the config price table and logged under `section='llm_eval'`
   (ISSUE_23) — the LLM eval is where real spend lands (≈30× an embedding token, per gpt-4o-mini
   output pricing).

6. **Enrich to the outcome — `core/pipeline/symbol_evaluator.py` (`SymbolEvaluator.evaluate`).**
   The engine wraps the scored fields into the full result: `symbol` (known), `sources` — the real
   retrieved articles as `ArticleRef[]` **provenance** (the LLM never invents article ids, ISSUE_2),
   and `is_breaking` from `urgency` vs the constellation's breaking threshold (the gate is *confirmed*
   here — the LLM read a real story as urgent, ISSUE_11). The returned `SymbolEval` also carries the
   **raw model output** (`completion.data`, ISSUE_36 — irreconstructable after the call; the outcome
   store persists it next to the normalized envelope, ISSUE_8) and the prompt's identity
   (`PromptMetadata`, ISSUE_33).

## Phase D — Outcome & envelope (assemble → persist → serve · planned)

Runs once per pipeline pass, over all requested symbols:

1. **Relevance floor — `core/rag/retriever.py` · *planned, ISSUE_24*.**
   Slots between retrieval and analysis: a symbol whose nearest article is beyond `min_similarity`
   (e.g. DASH/LTC with no dedicated news) yields an **empty** context → the envelope's
   HOLD/`0.0`/'No relevant news found' path, instead of a signal hallucinated from generic news.

2. **Assemble the envelope — `core/pipeline/pipeline_runner.py` (`PipelineRunner.run`) · built, ISSUE_7.**
   The staged flow in one readable top-down unit: ingest pass (inline in this first slice; moves to
   the ingest worker with ISSUE_10) → Phase B + C per symbol → assemble the `SentimentResult[]`
   into an `AnalysisEnvelope[SentimentResult]`. **Invariants:** every requested symbol is present
   (a failed symbol degrades to a clean `HOLD` row with its taxonomy-typed `RunError` — never a
   gap); `status: 'partial'` is preferred over `'error'` (`error` only when not a single symbol
   evaluated); the envelope is always parseable, even on internal failure (the API catches and
   answers `200` + `status: 'error'`). All stage timings, summed tokens, the run's USD (session
   delta off the shared `CostRecorder`) and per-symbol tokens fold into `RunMetadata`; the prompt
   fingerprint (`prompt_id@version` + `prompt_hash`, ISSUE_33) is stamped on the envelope.
   **Wiring:** `core/pipeline/pipeline_assembler.py` builds the per-pipeline object graph
   (sources → … → evaluator → ingestor) and attaches runners at API boot; `Pipeline` without a
   runner falls back to the scaffold mock (bootable without DB, and the free-suite path —
   contract tests never spend budget). The `run` CLI is the console twin of `POST /run`.

3. **Persist — `core/store/…` (`OutcomeStore`) · *planned, ISSUE_8*.**
   The produced envelope is the **source of truth**: persisted (DB / JSONL) before/independent of any
   live push, so runs are deterministically replayable with no look-ahead.

4. **Serve — API `/latest` · *planned, ISSUE_8*.**
   The eval worker writes; consumers read the cached latest envelope over HTTP (`/latest`), and
   `/run` triggers a pass. The IDE only ever reads the cached eval output.

5. **Collector handshake — JSONL + `collected_msc` · *planned, ISSUE_9*.**
   Downstream archives each envelope as one JSONL line plus a top-level `collected_msc` (int
   epoch-ms, the collector's receive time) — the no-look-ahead **merge key** (not the engine's own
   `timestamp`).

## What leaves the engine

A valid `AnalysisEnvelope` JSON — the generic shell (`schema_version`, `pipeline_id`, `outcome_type`,
`prompt_version`, `timestamp`, `status`) plus the per-symbol `SentimentResult[]` payload, `metadata`
(model, counts, timings, cost) and `errors`. Every consumer parses the same shell regardless of the
signal type; a new signal type = a new constellation + a new payload model, engine unchanged.

The two-worker split (ISSUE_10) later runs Phase A (ingest) and Phases B–D (eval) on independent
clocks over the one shared corpus; the retrieval + analysis flow above is unchanged by that split.
