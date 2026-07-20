"""Alert-channel errors (ISSUE_27) — delivery failures of the operator alert surface.

Deliberately outside the envelope taxonomy: a failed Telegram send degrades the
*reporting* channel, never a pipeline run — no envelope, no RunError. Callers log and
back off; the engine keeps producing.
"""
from finiexragengine.exceptions.ragengine_errors import FiniexRagError


class TelegramError(FiniexRagError):
    """Telegram Bot API call failed (HTTP error, bad token, API-level ok=false)."""
