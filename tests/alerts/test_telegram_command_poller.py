"""Telegram command poller (ISSUE_27) — chat restriction, commands, offset, backoff.

Async-loop idiom as in test_workers: fakes at the client seam, millisecond waits,
stop() after the scenario. No network.
"""
import asyncio
from typing import Any, Dict, List, Optional

from finiexragengine.core.alerts import telegram_command_poller
from finiexragengine.core.alerts.telegram_client import TelegramClient
from finiexragengine.core.alerts.telegram_command_poller import TelegramCommandPoller
from finiexragengine.exceptions.alert_errors import TelegramError
from finiexragengine.types.config_types.app_config_types import TelegramConfig

_CONFIG = TelegramConfig(enabled=True, bot_token='t', chat_id='4242',
                         poll_interval_seconds=1)


def _update(update_id: int, text: str, chat: str = '4242') -> Dict[str, Any]:
    return {'update_id': update_id,
            'message': {'chat': {'id': chat}, 'text': text}}


class FakeClient(TelegramClient):
    """Client fake: serves queued update batches, then blocks until cancelled."""

    def __init__(self, batches: List[Any]) -> None:   # no super(): no HTTP client needed
        self.batches = list(batches)
        self.sent: List[str] = []

    async def get_updates(self, offset: Optional[int],
                          timeout_seconds: int) -> List[Dict[str, Any]]:
        if self.batches:
            batch = self.batches.pop(0)
            if isinstance(batch, Exception):
                raise batch
            return batch
        await asyncio.Event().wait()                  # idle long-poll — until stop()
        return []

    async def send_message(self, text: str) -> None:
        self.sent.append(text)

    async def send_messages(self, texts: List[str]) -> None:
        self.sent.extend(texts)


async def _run_scenario(client: FakeClient, built: Optional[List[str]] = None,
                        ) -> TelegramCommandPoller:
    async def build() -> List[str]:
        return built if built is not None else ['weekly part 1']

    poller = TelegramCommandPoller(client, _CONFIG, build)
    await poller.start()
    await asyncio.sleep(0.05)
    await poller.stop()
    return poller


def test_report_command_builds_and_sends_offset_advances():
    client = FakeClient([[_update(7, '/report')]])
    poller = asyncio.run(_run_scenario(client, built=['weekly part 1', 'part 2']))
    assert client.sent == ['weekly part 1', 'part 2']
    assert poller._offset == 8                        # consumed → next poll skips it


def test_foreign_chat_is_consumed_but_ignored():
    client = FakeClient([[_update(3, '/report', chat='999')]])
    poller = asyncio.run(_run_scenario(client))
    assert client.sent == []
    assert poller._offset == 4                        # still consumed — no replay loop


def test_help_answers_and_other_text_is_ignored():
    client = FakeClient([[_update(1, 'hello'), _update(2, '/help')]])
    asyncio.run(_run_scenario(client))
    assert len(client.sent) == 1 and '/report' in client.sent[0]


def test_configured_command_matches_exactly_not_by_prefix():
    cfg = TelegramConfig(enabled=True, bot_token='t', chat_id='4242',
                         poll_interval_seconds=1, report_command='/report-rag')

    async def scenario() -> None:
        # The distinct RAG command fires; a bare /report (the collector's) and a prefix
        # near-miss are both ignored — exact-token match, tolerating a @Bot suffix.
        client = FakeClient([[_update(1, '/report'), _update(2, '/reportx'),
                              _update(3, '/report-rag@FiniexBot')]])

        async def build() -> List[str]:
            return ['rag weekly']

        poller = TelegramCommandPoller(client, cfg, build)
        await poller.start()
        await asyncio.sleep(0.05)
        await poller.stop()
        assert client.sent == ['rag weekly']          # only /report-rag triggered

    asyncio.run(scenario())


def test_build_failure_sends_a_notice_and_loop_survives(monkeypatch):
    monkeypatch.setattr(telegram_command_poller, '_BACKOFF_START', 0.001)

    async def scenario() -> None:
        client = FakeClient([[_update(1, '/report')], [_update(2, '/help')]])

        async def build() -> List[str]:
            raise RuntimeError('db down')

        poller = TelegramCommandPoller(client, _CONFIG, build)
        await poller.start()
        await asyncio.sleep(0.05)
        await poller.stop()
        assert any('report failed' in text for text in client.sent)   # notice, not silence
        assert any('/report' in text for text in client.sent)         # loop kept serving

    asyncio.run(scenario())


def test_poll_error_backs_off_and_recovers(monkeypatch):
    monkeypatch.setattr(telegram_command_poller, '_BACKOFF_START', 0.001)

    async def scenario() -> None:
        client = FakeClient([TelegramError('HTTP 502'), [_update(5, '/help')]])
        poller = TelegramCommandPoller(client, _CONFIG, _fail_never)
        await poller.start()
        await asyncio.sleep(0.05)
        await poller.stop()
        assert len(client.sent) == 1                  # recovered after the failed poll

    async def _fail_never() -> List[str]:
        return []

    asyncio.run(scenario())
