# Prompt & LLM Stage (analysis)

How the retrieved context becomes a typed signal (ISSUE_6). The retrieval squeeze hands a small,
on-topic article set to the LLM, which scores the symbol's sentiment; the engine then wraps that
score with provenance into the outcome envelope.

## Prompt templates (versioned Jinja2 / Markdown files)

Prompts live as **Jinja2 Markdown** files under `prompts/`, named `<name>_v<version>.md`
(e.g. `prompts/sentiment_v1.md`). Each **pipeline declares which prompt it uses** (ISSUE_33) via a
`prompt` block in its constellation JSON — so prompts are swappable per pipeline without touching
code:

```json
"prompt": { "name": "sentiment", "version": "1" }
```

`PromptBuilder` (`core/llm/prompt_builder.py`) resolves that to `prompts/sentiment_v1.md` and renders
the template with `symbol` and the retrieved `articles`.

- The article-rendering **loop lives in the template** (`{% for a in articles %}`) and the
  empty-context fallback is a template `{% if %}` — so prompt wording *and* formatting stay in one
  reviewable file, out of Python. Markdown keeps it readable (GitHub-rendered), and LLMs parse the
  structure (headings / lists) well.
- The render context is `symbol`, `articles`, and **`now`** (timezone-aware UTC wall clock) — the
  "current time" anchor without which article timestamps are useless for age-weighting. **Ordering
  is template-owned** (v2 sorts newest-first via `|sort(attribute='published_at', reverse=true)`):
  presentation to the LLM is prompt behavior, so it stays versioned and hash-visible. v2 also
  surfaces each article's **source trust score** (`source_weight`, the operator's
  seriousness/reliability rating from the constellation's `sources[]`) with a one-line
  instruction to weigh accordingly.
- The Jinja2 env is `autoescape=False` (raw prompt text, not HTML) with `StrictUndefined` (a typo'd
  template variable fails loudly, not silently empty).
- **Bump the version when the prompt changes** — different prompts score the same news differently,
  so the consumer must keep the series apart (replay/backfill). A bump = a new file.

## Prompt metadata & reproducibility (ISSUE_33)

Every prompt is set in stone per version and carries a **YAML front-matter** block the builder parses
into `PromptMetadata` (`types/prompt_metadata.py`) — the `---` block never leaks into the rendered
prompt:

```markdown
---
id: sentiment-crypto
version: 1
author: FiniexRAGEngine
created: 2026-07-09
description: Crypto fear/greed sentiment scoring from retrieved news articles
---
You are a crypto-market sentiment analyst...
```

- `content_hash` is a short SHA-256 of the **template body** (front-matter excluded). A
  behaviour-changing edit moves the hash — a **silent prompt change is visible in the output**; a
  cosmetic metadata fix (author typo) does *not* move it, so the series stays intact.
- The front-matter is optional-tolerant: a template without a `---` block falls back to `id = name`,
  `version =` the requested version, empty author/created/description (no hard fail for legacy prompts).
- The outcome envelope records **`prompt_id` + `prompt_version` + `prompt_hash`** alongside the
  score, so a consumer can tell exactly which prompt produced it. Those fields are filled when the
  envelope is assembled (ISSUE_7); the `eval` CLI already surfaces the identity as
  `prompt <id>@v<version> #<hash>`. Parsed templates are cached (a prompt is immutable per version).

## Structured output

`OpenAIProvider.complete_structured(prompt, json_schema)` (`core/llm/openai_provider.py`) calls
chat-completions with a `response_format` JSON schema, low `temperature` + `timeout` from `LlmConfig`,
and returns an `LlmCompletion(data, usage)`.

- The LLM returns **only the scored fields** — `SentimentLlmOutput`: `signal`, `sentiment_score`,
  `confidence`, `reasoning`, `urgency`. **Provenance (`sources`), `is_breaking` and `symbol` are
  attached by the engine** from the actual retrieved articles; the model never invents article ids.
- `SentimentLlmOutput` is strict (`extra='forbid'`, all fields required), so a malformed completion
  is rejected on validation.

## Model governance (per-pipeline model + allowlist)

The eval model is **series-defining, exactly like the prompt**: a different model yields
different scores for the same news. Its declaration is therefore two-level:

- **Each pipeline declares its model — required.** `"llm": { "model": "gpt-4o-mini" }` in the
  constellation; there is deliberately **no global default model**, so an app-config edit can
  never silently retarget every pipeline's signal series at once.
- **`app_config.llm.allowed_models` is the governance allowlist.** A pipeline requesting a model
  outside it fails at assembly (`ConfigurationError`) — fail fast, before any spend. The list is
  overridable in the gitignored `user_configs` (replaced wholesale), which is also how a
  **fine-tuned model** enters: allowlist its `ft:...` id there, point a pipeline at it, add a
  pricing entry — done. Self-hosted OpenAI-compatible endpoints (vLLM, Ollama) plug in via
  `llm.base_url` (user_configs — private infrastructure).
- **The served model is captured per call.** The configured name (`gpt-4o-mini`) is an *alias*
  the provider retargets silently to dated snapshots; every response reports the actual one
  (`response.model`). It lands in `cost_log.model_snapshot` and the envelope's
  `metadata.model_snapshot` — a silent snapshot switch becomes visible in the series, exactly
  like a `prompt_hash` change (the model-side half of reproducibility, ISSUE_33). Pricing keys
  on the configured alias; the snapshot is the trace.
- **Availability is checked, staged like the run.** `core/llm/model_catalog.py` verifies the
  configured ids against the provider's live list (`models.list`, free) in two sections:
  **ingest** — the embedding model, which is corpus-binding (#16): if it vanishes, ingest *and*
  query embedding fail with no substitute short of re-embedding the corpus — and **llm stage** —
  every `allowed_models` entry. Runs **softly at server boot** (warn, never block; the allowlist
  stays the hard gate) and manually via `models_cli`. With a custom `llm.base_url` the eval
  models are checked against that endpoint while the embedding model stays checked against the
  OpenAI default. The embed call also captures the served model (`response.model`) into
  `cost_log.model_snapshot` — embedding ids carry no alias/snapshot pair (the id *is* the
  version), but if one were ever retargeted the alias-drift warning would fire for ingest too.
- **The provider is a seam.** The eval flow depends only on `AbstractLLMProvider`;
  `llm.provider` names the implementation, resolved by `core/llm/provider_factory.py` (the
  `source_factory` mirror — an unknown name fails at assembly). A genuinely different API
  protocol = a new provider class + factory entry; OpenAI-compatible endpoints (vLLM, Ollama,
  fine-tunes) are **not** one — they ride the existing class via `base_url` / the model string.

## Errors & cost

Failures map to the taxonomy: timeout → `LLMTimeoutError` (LLM_TIMEOUT), backend error →
`LLMApiError` (LLM_API_ERROR), non-JSON output → `LLMParseError` (LLM_PARSE_ERROR) — all rooted at
`LLMError`. Token `usage` is captured on every call and, when a `CostRecorder` is set, logged under
`section='llm_eval'` (ISSUE_23) — the LLM eval is where real spend appears. The call's `duration_ms`
rides the same row (ISSUE_32), so the cost log doubles as the API-latency log (`perf_cli`).

Wiring this stage into `Pipeline.run` (retrieve → build → complete → assemble envelope) is **ISSUE_7**.
