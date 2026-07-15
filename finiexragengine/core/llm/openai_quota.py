"""OpenAI error classification — vendor knowledge, kept behind the provider seam.

CLAUDE.md: *provider-specific behavior stays inside the concrete provider*. The cost
circuit-breaker (`BudgetGuard`, ISSUE_47) is deliberately provider-agnostic — it suspends, backs
off, re-probes and resumes without knowing whose quota ran out. **Deciding** that a given
exception *is* a quota stop is the opposite: it reads one vendor's error vocabulary. So it lives
here, next to the OpenAI units that raise it (`OpenAIProvider`, `OpenAIEmbedder` — the embedder
rides the same SDK), not inside the guard. A second provider brings its own classifier; the guard
stays untouched.

Duck-typed on purpose (`getattr`, no `openai` import): a leaf module both call-sites can pull in
for free, and it stays testable with plain fakes.
"""


def is_quota_exceeded(exc: Exception) -> bool:
    """True for a budget/quota stop, False for a transient rate-limit.

    OpenAI returns HTTP 429 for both; they differ by the error `code`. A rate-limit
    (`rate_limit_exceeded`) is a short throttle (retryable); the account spend limit is
    `insufficient_quota` (verified live). Any *other* 429 is treated as a budget stop too — a 429
    on OpenAI is only ever a rate-limit or a budget/quota stop — with a billing/quota message
    fallback for a non-429 billing error. Robust to a differently-coded spend limit.
    """
    code = getattr(exc, 'code', None)
    if code == 'rate_limit_exceeded':
        return False
    if code == 'insufficient_quota':
        return True
    if getattr(exc, 'status_code', None) == 429:
        return True
    message = str(getattr(exc, 'message', '') or exc).lower()
    return any(k in message for k in ('quota', 'billing', 'spend limit', 'exceeded your current'))
