"""The one credential vocabulary for everything this engine publishes (2026-09-08).

Two read surfaces now emit text an operator did not write by hand: `GET /v1/logs/{name}` serves log
lines, `GET /v1/configs/{name}` serves configuration documents. Both can carry a secret that reached
them by accident — a DSN echoed by a psycopg traceback, an `Authorization` header in an HTTP client's
stack, a feed URL whose query string holds its own API key.

It lives in `utils/` because a second copy of a security vocabulary is worse than none: the copy that
is not updated is the one that leaks, and nothing would fail when they drift. `utils/` is the
dependency-free layer, so every caller can reach it without an import cycle — the config views under
`configuration/` must not import from `core/`, which is where this started life.

**What it is not.** These are patterns for shapes, not a heuristic for "looks secret". A heuristic
would either miss `chat_id` or redact half a log, and the caller that knows a field is a credential
should say so by path (`configuration/config_redaction.py`) rather than hoping a regex agrees. This
is the second layer, and its job is the value that arrives somewhere nobody classified.
"""
import re
from typing import Pattern, Tuple

# Each pattern is here because a real surface produced that shape, and the comment says which.
_REDACTIONS: Tuple[Tuple[str, Pattern], ...] = (
    # A psycopg failure can echo the DSN it was given, password and all.
    ('dsn', re.compile(r'(?P<head>[a-z+]+://[^\s:/@]+:)[^\s@]+(?P<tail>@)')),
    # An HTTP client traceback can carry the Authorization header it sent.
    ('bearer', re.compile(r'(?i)(?P<head>bearer\s+)[A-Za-z0-9._\-]{8,}')),
    # OpenAI keys, in a URL or a repr.
    ('openai_key', re.compile(r'sk-[A-Za-z0-9._\-]{8,}')),
    # Telegram puts the bot token in the PATH, so any URL echo leaks it.
    ('telegram_token', re.compile(r'(?P<head>/bot)\d{6,}:[A-Za-z0-9_\-]{8,}')),
    # A feed URL can carry its own key as a query parameter. Configs have this shape and logs do
    # not, which is exactly why the vocabulary is shared: the pattern is written once and both
    # surfaces gain it. The parameter NAME survives — `?apikey=«redacted»` still says what kind of
    # feed this is, which is the diagnostic value.
    ('url_query_secret',
     re.compile(r'(?i)(?P<head>[?&](?:api_?key|access_?key|auth|token|secret|password|pwd)=)'
                r'[^&\s#]+')),
)
# Public: the config views mask by policy rather than by pattern and must write the
# same token, or a reader would have to learn two spellings for one fact.
MASK = '«redacted»'


def redact(text: str) -> Tuple[str, bool]:
    """Mask anything credential-shaped; report whether the text was changed.

    The boolean is not a convenience — every caller is expected to surface it. A reader trusts what
    a diagnostic surface hands them, so a line that was altered without saying so is worse than one
    that was withheld.
    """
    out = text
    for _name, pattern in _REDACTIONS:
        out = pattern.sub(
            lambda match: ''.join(filter(None, (match.groupdict().get('head'), MASK,
                                                match.groupdict().get('tail')))),
            out)
    return out, out != text
