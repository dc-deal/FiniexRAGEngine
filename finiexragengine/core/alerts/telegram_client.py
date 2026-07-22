"""Thin async Telegram Bot API client (ISSUE_27) — send + long-poll, nothing more.

Uses httpx (already the project's HTTP client — deliberately no aiohttp, see the issue
fold-back): one client, two endpoints. The token travels only inside the request URL and
is never logged; error texts carry the HTTP status / API description, not the URL.
"""
import logging
from typing import Any, Dict, List, Optional

import httpx

from finiexragengine.exceptions.alert_errors import TelegramError
from finiexragengine.types.config_types.app_config_types import TelegramConfig

logger = logging.getLogger(__name__)

_API_BASE = 'https://api.telegram.org'
# Telegram messages cap at 4096 chars — the renderer packs below this; the client only
# guards against an oversized stray text (hard truncation is better than a lost report).
_MAX_MESSAGE = 4096


def _reason(exc: httpx.HTTPError) -> str:
    """Human, token-safe cause for a transport failure — never `str(exc)` (it may embed the
    URL, hence the token). The class name is kept in parentheses for diagnostics."""
    kind = type(exc).__name__
    if isinstance(exc, httpx.ConnectError):
        # The common one: no network / DNS did not resolve api.telegram.org.
        return f'could not reach {_API_BASE} — network/DNS ({kind})'
    if isinstance(exc, httpx.TimeoutException):
        return f'timed out reaching {_API_BASE} ({kind})'
    return kind


class TelegramClient:
    """Bot-API mechanics for one bot + one chat (the configured operator chat)."""

    def __init__(self, config: TelegramConfig,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self._config = config
        # Injectable transport for tests (httpx.MockTransport); owned when self-built.
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(self, text: str) -> None:
        """Send one HTML-formatted message to the configured chat."""
        await self._call('sendMessage', {
            'chat_id': self._config.chat_id,
            'text': text[:_MAX_MESSAGE],
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        })

    async def send_messages(self, texts: List[str]) -> None:
        """Send a multi-part report in order (the renderer splits at section bounds)."""
        for text in texts:
            await self.send_message(text)

    async def get_updates(self, offset: Optional[int],
                          timeout_seconds: int) -> List[Dict[str, Any]]:
        """Long-poll for updates — returns the raw update dicts (poller interprets them)."""
        params: Dict[str, Any] = {'timeout': timeout_seconds,
                                  'allowed_updates': ['message']}
        if offset is not None:
            params['offset'] = offset
        # Read timeout must outlive the server-side long-poll window.
        return await self._call('getUpdates', params,
                                timeout=httpx.Timeout(timeout_seconds + 10.0))

    async def _call(self, method: str, payload: Dict[str, Any],
                    timeout: Optional[httpx.Timeout] = None) -> Any:
        url = f'{_API_BASE}/bot{self._config.bot_token}/{method}'
        try:
            response = await self._client.post(url, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            # str(exc) may embed the URL (and so the token) — build the reason from the type.
            raise TelegramError(f'{method} failed: {_reason(exc)}') from exc
        if response.status_code != 200:
            # Telegram puts the reason in the JSON body even on a non-200 (e.g. a 409
            # `Conflict: terminated by other getUpdates request` when a second poller
            # shares the bot) — surface it so the log names the cause, not just the code.
            detail = ''
            try:
                detail = f": {response.json().get('description', '')}".rstrip(': ')
            except ValueError:
                pass
            raise TelegramError(f'{method} failed: HTTP {response.status_code}{detail}')
        body = response.json()
        if not body.get('ok'):
            raise TelegramError(f"{method} failed: {body.get('description', 'not ok')}")
        return body.get('result')
