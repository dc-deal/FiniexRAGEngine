# Detailed Analysis Stage & Outcome

Picks up where `01_ingest_and_retrieval.md` leaves off. Retrieval has handed a small,
on-topic `List[Article]` **per symbol**; here that context becomes a typed signal, and the
per-symbol signals are assembled into the outcome envelope that leaves the engine.

**Status:** Phase C (analysis) is **built** (ISSUE_6), Phase D's assembly is **built**
(ISSUE_7 — `PipelineRunner`), and persist → serve is **built** (ISSUE_8 — `OutcomeStore` +
`/latest`): `POST /run` executes the real staged flow and persists its envelope; `GET /latest`
serves the persisted outcome instantly (without `DATABASE_URL` the scaffold mock still answers).
The collector handshake remains ISSUE_9.

Companion docs: `01_ingest_and_retrieval.md` (the write + read paths) and
`../prompt_and_llm_stage.md` (the LLM stage in depth).

## Phase C — Analysis (per symbol, built · ISSUE_6)

Top-down, one symbol's retrieved context flows through:

1. **Context in — `core/rag/retriever.py` (`Retriever.retrieve`).**
   The output of Phase B: at most `top_k` distinct, recent, on-topic `Article`s for this symbol
   (the comparison scores were already dropped — only the selected articles travel on).

2. **Build the prompt — `core/llm/prompt_builder.py` (`PromptBuilder.build`).**
   Renders the versioned Jinja2 Markdown template `prompts/<name>/<name>_v<version>.md`
   (one folder per prompt family — crypto_sentiment/, forex_sentiment/): fills
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

1. **Relevance floor + no_data shortcut — built, ISSUE_24.**
   The retriever drops candidates beyond `retrieval.floor_distance` (see
   `01_ingest_and_retrieval.md`, step 4); a symbol with only generic coverage (e.g. LTC)
   yields an **empty** context. `SymbolEvaluator.evaluate` then answers **mechanically** —
   the contract row `HOLD / 0.0 / 'No relevant news found' / []`, tagged
   **`basis='no_data'`** (machine-readable: no evaluation possible due to data shortage) —
   **without building a prompt or paying an LLM call**. Logged as `[NO_CONTEXT]` for
   traceability; deliberately *not* a `RunError` (no data is a legitimate outcome, the run
   stays `success`), and the envelope proves it regardless: 0 tokens for the symbol, empty
   raw output. Failure- and guard-degraded rows carry `basis='degraded'` instead (next step).

2. **Output consistency guard — `core/pipeline/output_guard.py` (`OutputGuard`) · built, ISSUE_35.**
   Schema validation (Phase C step 4) proves a completion is well-formed and in range — this
   deterministic, zero-cost check proves it *coherent*. Rules: a directional signal must not
   sit on the wrong side of zero beyond `output_guard.score_signal_tolerance` (dead zone); a
   no-signal HOLD must not carry confidence above `hold_confidence_max`; `reasoning`
   non-empty; a directional signal cites sources (structural backstop — the engine attaches
   them itself). The runner applies it right after a successful evaluation: a violated row
   degrades **in place** to the contract HOLD (`basis='degraded'`, urgency 0 — it can never
   push breaking), recorded as a `PARTIAL_RESPONSE` `RunError` → run `status='partial'`. The
   `SymbolEval` keeps its tokens/cost/timings and its **raw model output** (ISSUE_36) — only
   the served row is swapped, so a degraded completion stays inspectable. The boundary is
   deliberate: whether the reasoning *matches the articles* would need an LLM judge — a
   second paid call, out of scope; a *gated* escalation is ISSUE_30's territory.

3. **Assemble the envelope — `core/pipeline/pipeline_runner.py` (`PipelineRunner.run`) · built, ISSUE_7.**
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

4. **Persist — `core/store/outcome_store.py` (`OutcomeStore`) · built, ISSUE_8 + ISSUE_36.**
   The pass ends with persistence: the runner saves the produced envelope into a Postgres
   table alongside pgvector (one JSONB column = the exact served JSON, plus thin
   `pipeline_id`/`ts`/`status` query columns) — the **source of truth** for replay and for
   error statistics (aggregated from persisted envelopes' `status`/`errors`, never from
   logs). The **raw per-symbol LLM output** rides in its own JSONB column on the same row
   (ISSUE_36, irreconstructable after the call; explicitly non-load-bearing — never bumps
   `schema_version`): raw output ↔ normalized result ↔ prompt fingerprint = a fully
   reconstructable run. A store failure never loses the envelope for the caller — the pass
   degrades (`VECTOR_STORE_ERROR`, `success` → `partial`) and is still served. The API's
   catch-all error envelope is persisted best-effort too, so even a crashed pass is a row.

5. **Serve — API `/latest` · built, ISSUE_8.**
   `/latest` reads the newest persisted envelope (one indexed point read — instant, zero
   spend; ~27ms vs ~6.5s for a fresh pass, surviving restarts); `/run` triggers a fresh pass
   (which persists itself). Cold miss — nothing persisted yet — runs once, then serves that.
   The IDE only ever reads the cached eval output.

6. **Collector handshake — JSONL + `collected_msc` · *planned, ISSUE_9*.**
   Downstream archives each envelope as one JSONL line plus a top-level `collected_msc` (int
   epoch-ms, the collector's receive time) — the no-look-ahead **merge key** (not the engine's own
   `timestamp`).

## What leaves the engine

A valid `AnalysisEnvelope` JSON — the generic shell (`schema_version`, `pipeline_id`, `outcome_type`,
`prompt_version`, `timestamp`, `status`) plus the per-symbol `SentimentResult[]` payload, `metadata`
(model, counts, timings, cost) and `errors`. Every consumer parses the same shell regardless of the
signal type; a new signal type = a new constellation + a new payload model, engine unchanged.

The two-worker split (ISSUE_10, **built**) runs Phase A (ingest) and Phases B–D (eval) on
independent clocks over the one shared corpus — `server_cli --workers` starts one ingest worker
per referenced source-set and one eval worker per logical pipeline (fan variants included); the
retrieval + analysis flow above is unchanged by the split, worker-mode eval passes simply skip
the inline Phase A (`PipelineRunner(ingestor=None)`).

**Breaking priority path (ISSUE_11, built).** The eval worker runs on its interval **or** on a
breaking wake: when the ingest detector flags a candidate, the `BreakingBus` nudges the eval
workers on that set whose `breaking.min_importance` the flagged tier reaches, and the `EventTrigger`
runs an off-cadence pass in seconds. The pass is otherwise unchanged — the **confirm gate** is the
existing `is_breaking = urgency ≥ breaking.urgency_threshold` (SymbolEvaluator step 6); a breaking
wake only makes eval run *sooner*, not differently, so the envelope stays model/fingerprint-
consistent. Everything is persisted regardless; the gate governs only what would *push* (Stage C).
Two knobs, two stages (wake vs confirm) — see `../breaking_detection.md`.
