# Prompt & LLM Stage (analysis)

How the retrieved context becomes a typed signal (ISSUE_6). The retrieval squeeze hands a small,
on-topic article set to the LLM, which scores the symbol's sentiment; the engine then wraps that
score with provenance into the outcome envelope.

## Prompt templates (versioned Jinja2 / Markdown files)

Prompts live as **Jinja2 Markdown** files under `prompts/`, named `<name>_v<prompt_version>.md`
(e.g. `prompts/sentiment_v1.md`), selected by the constellation's `prompt_version`. `PromptBuilder`
(`core/llm/prompt_builder.py`) renders the template with `symbol` and the retrieved `articles`.

- The article-rendering **loop lives in the template** (`{% for a in articles %}`) and the
  empty-context fallback is a template `{% if %}` — so prompt wording *and* formatting stay in one
  reviewable file, out of Python. Markdown keeps it readable (GitHub-rendered), and LLMs parse the
  structure (headings / lists) well.
- The Jinja2 env is `autoescape=False` (raw prompt text, not HTML) with `StrictUndefined` (a typo'd
  template variable fails loudly, not silently empty).
- **Bump `prompt_version` when the prompt changes** — different prompts score the same news
  differently, so the consumer must keep the series apart (replay/backfill). A bump = a new file.

## Structured output

`OpenAIProvider.complete_structured(prompt, json_schema)` (`core/llm/openai_provider.py`) calls
chat-completions with a `response_format` JSON schema, low `temperature` + `timeout` from `LlmConfig`,
and returns an `LlmCompletion(data, usage)`.

- The LLM returns **only the scored fields** — `SentimentLlmOutput`: `signal`, `sentiment_score`,
  `confidence`, `reasoning`, `urgency`. **Provenance (`sources`), `is_breaking` and `symbol` are
  attached by the engine** from the actual retrieved articles; the model never invents article ids.
- `SentimentLlmOutput` is strict (`extra='forbid'`, all fields required), so a malformed completion
  is rejected on validation.

## Errors & cost

Failures map to the taxonomy: timeout → `LLMTimeoutError` (LLM_TIMEOUT), backend error →
`LLMApiError` (LLM_API_ERROR), non-JSON output → `LLMParseError` (LLM_PARSE_ERROR) — all rooted at
`LLMError`. Token `usage` is captured on every call and, when a `CostRecorder` is set, logged under
`section='llm_eval'` (ISSUE_23) — the LLM eval is where real spend appears.

Wiring this stage into `Pipeline.run` (retrieve → build → complete → assemble envelope) is **ISSUE_7**.
