"""Telegram command loop (ISSUE_27) — `/report` and `/help`, on demand.

Long-polls the Bot API (`getUpdates` + offset) as a background asyncio task in the API
process. Hardened like the workers' pass loop: every failure is caught, logged and backed
off — the loop never kills the app. Only the configured operator chat is served; updates
from any other chat advance the offset (so they are consumed) but are ignored.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from finiexragengine.core.alerts.telegram_client import TelegramClient
from finiexragengine.types.config_types.app_config_types import TelegramConfig

logger = logging.getLogger(__name__)

# The on-demand report: build + render, returns the ready-to-send messages.
BuildReport = Callable[[], Awaitable[List[str]]]

_BACKOFF_START = 1.0
_BACKOFF_MAX = 300.0


class TelegramCommandPoller:

    def __init__(self, client: TelegramClient, config: TelegramConfig,
                 build_report: BuildReport) -> None:
        self._client = client
        self._config = config
        self._build_report = build_report
        self._offset: Optional[int] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        # Cancel cuts the in-flight long-poll; the loop re-raises CancelledError cleanly.
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        backoff = _BACKOFF_START
        while True:
            try:
                updates = await self._client.get_updates(
                    self._offset, self._config.poll_interval_seconds)
                backoff = _BACKOFF_START
                for update in updates:
                    self._offset = update['update_id'] + 1
                    await self._handle(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Network blips and API hiccups are expected across a live week — log the
                # actual reason (a 409 from a second poller on a shared bot reads clearly),
                # back off (capped), keep polling.
                logger.warning('telegram poll failed: %s — retry in %.0fs', exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _handle(self, update: Dict[str, Any]) -> None:
        message = update.get('message') or {}
        chat_id = str((message.get('chat') or {}).get('id', ''))
        if chat_id != self._config.chat_id:
            return                                    # not the operator chat — consumed, ignored
        text = (message.get('text') or '').strip()
        # Match the command word exactly, tolerating a `/cmd@BotName` group suffix — so a
        # configured `/report-rag` is distinct from `/report` (no prefix false-match).
        command = (text.split() or [''])[0].split('@')[0]
        if command == self._config.report_command:
            logger.info('telegram %s requested', command)
            try:
                await self._client.send_messages(await self._build_report())
            except Exception:
                logger.exception('on-demand report failed')
                await self._send_quietly('⚠ report failed — check the engine logs')
        elif command == '/help':
            await self._send_quietly(
                '🤖 <b>FiniexRAGEngine</b>\n'
                f'{self._config.report_command} — the weekly report, built now\n'
                '/help — this message')

    async def _send_quietly(self, text: str) -> None:
        """Best-effort notice — a failing send must not take the poll loop down."""
        try:
            await self._client.send_message(text)
        except Exception:
            logger.warning('telegram notice could not be sent')
