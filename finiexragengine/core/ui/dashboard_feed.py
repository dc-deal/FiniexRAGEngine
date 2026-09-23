"""The viewer's half of the wire: fetch one reading, or state why there is none (ISSUE_126).

A viewer draws numbers measured on another machine, and the moment the connection drops every one of
them describes the past while looking exactly as current as it did a second earlier. That is the
same defect a producer commits when it writes a field it does not know, moved onto a screen — so
this unit never returns a half-answer. It returns a reading, or a condition in words.

**The reason is a sentence, not a code**, because the common failures send an operator to different
machines: a refused connection means the engine is not running, a timeout means it is running and
not answering, `401` means the credential, `403` means the credential is fine and a grant is missing,
`503` means the engine is up and collecting nothing. Four different places to go, and a bare number
sends you to none of them.

**The timeout floor is not arbitrary.** A refused TCP connection took **2.04 s** to report itself on
the Windows box this engine runs on, because the stack retries the SYN first; the collector's 2.0 s
timeout therefore reported a service that was simply not running as "no answer within the timeout" —
the wrong sentence, by forty milliseconds. Ours starts at five seconds.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from finiexragengine.core.ui.remote_engine_state import RemoteEngineState

logger = logging.getLogger(__name__)

# Above the 2.04 s a refused connection needs to report itself on that host — see the module note.
_DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class FeedReading:
    """Either a reading or a reason, and never both — `state` is None exactly when `reason` is set."""
    at: datetime
    state: Optional[RemoteEngineState] = None
    reason: str = ''

    @property
    def ok(self) -> bool:
        return self.state is not None


class DashboardFeed:
    """Polls one engine's dashboard route. Holds no state beyond its address and credential."""

    def __init__(self, base_url: str, token: str, *,
                 view: str = 'engine',
                 timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self._url = f'{base_url.rstrip("/")}/v1/dashboard/{view}'
        self._token = token
        self._timeout = max(timeout_seconds, _DEFAULT_TIMEOUT_SECONDS)

    def url(self) -> str:
        return self._url

    def poll(self) -> FeedReading:
        """One fetch. Never raises — a viewer that dies on a network blip is worse than a stale one."""
        now = datetime.now(timezone.utc)
        try:
            response = httpx.get(self._url, timeout=self._timeout,
                                 headers={'Authorization': f'Bearer {self._token}'})
        except httpx.ConnectError:
            return FeedReading(at=now, reason='connection refused — the engine is not running')
        except httpx.TimeoutException:
            return FeedReading(at=now,
                               reason=f'no answer within {self._timeout:.0f}s — the engine is '
                                      f'running and not answering')
        except httpx.HTTPError as exc:
            return FeedReading(at=now, reason=f'transport failed — {exc.__class__.__name__}')

        if response.status_code != 200:
            return FeedReading(at=now, reason=_explain(response))
        try:
            return FeedReading(at=now, state=RemoteEngineState.from_payload(response.json()))
        except (ValueError, TypeError) as exc:
            # A payload this viewer cannot rebuild is a stated condition, not a traceback: the
            # likeliest cause is an engine older than this checkout, and `from_jsonable` names the
            # field it could not fill.
            return FeedReading(at=now, reason=f'unreadable payload — {exc}')


def _explain(response: httpx.Response) -> str:
    """The status, as the sentence that says which machine to go to."""
    if response.status_code == 401:
        return '401 — the credential is wrong or missing'
    if response.status_code == 403:
        return '403 — the credential is fine, the grant for this view is missing'
    if response.status_code == 404:
        return '404 — this engine serves no such view'
    if response.status_code == 503:
        detail = ''
        try:
            detail = response.json().get('detail', '')
        except ValueError:
            pass
        return f'503 — {detail or "the engine is up and collecting nothing"}'
    return f'HTTP {response.status_code} — unexpected, and the engine answered it'
