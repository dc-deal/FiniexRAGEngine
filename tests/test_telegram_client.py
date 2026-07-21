"""Telegram client (ISSUE_27) — Bot-API mechanics against a mock transport, no network."""
import asyncio
import json

import httpx
import pytest

from finiexragengine.core.alerts.telegram_client import TelegramClient
from finiexragengine.exceptions.alert_errors import TelegramError
from finiexragengine.types.config_types.app_config_types import TelegramConfig

_CONFIG = TelegramConfig(enabled=True, bot_token='sekret-token', chat_id='4242')


def _client(handler) -> TelegramClient:
    transport = httpx.MockTransport(handler)
    return TelegramClient(_CONFIG, client=httpx.AsyncClient(transport=transport))


def test_send_message_posts_chat_and_html():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['path'] = request.url.path
        seen['payload'] = json.loads(request.content)
        return httpx.Response(200, json={'ok': True, 'result': {}})

    asyncio.run(_client(handler).send_message('<b>hi</b>'))
    assert seen['path'].endswith('/botsekret-token/sendMessage')
    assert seen['payload']['chat_id'] == '4242'
    assert seen['payload']['text'] == '<b>hi</b>'
    assert seen['payload']['parse_mode'] == 'HTML'


def test_send_messages_keeps_order():
    texts = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)['text'])
        return httpx.Response(200, json={'ok': True, 'result': {}})

    asyncio.run(_client(handler).send_messages(['part 1', 'part 2']))
    assert texts == ['part 1', 'part 2']


def test_http_error_surfaces_status_and_description_without_leaking_the_token():
    # Telegram carries the reason in the body even on a non-200 (e.g. a 409 conflict when
    # a second poller shares the bot) — the error names both the code and the description.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            'ok': False, 'error_code': 409,
            'description': 'Conflict: terminated by other getUpdates request'})

    with pytest.raises(TelegramError) as err:
        asyncio.run(_client(handler).get_updates(None, timeout_seconds=1))
    assert 'HTTP 409' in str(err.value)
    assert 'Conflict: terminated by other getUpdates request' in str(err.value)
    assert 'sekret' not in str(err.value)


def test_http_error_without_body_still_reports_the_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='nope')      # not JSON

    with pytest.raises(TelegramError) as err:
        asyncio.run(_client(handler).send_message('x'))
    assert 'HTTP 401' in str(err.value)
    assert 'sekret' not in str(err.value)


def test_api_level_not_ok_raises_with_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'ok': False, 'description': 'chat not found'})

    with pytest.raises(TelegramError, match='chat not found'):
        asyncio.run(_client(handler).send_message('x'))


def test_transport_error_reports_only_the_exception_class():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('boom https://api.telegram.org/botsekret-token/x')

    with pytest.raises(TelegramError) as err:
        asyncio.run(_client(handler).send_message('x'))
    assert 'ConnectError' in str(err.value)
    assert 'sekret' not in str(err.value)


def test_get_updates_passes_offset_and_returns_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload['offset'] == 7
        return httpx.Response(200, json={'ok': True, 'result': [{'update_id': 7}]})

    updates = asyncio.run(_client(handler).get_updates(7, timeout_seconds=1))
    assert updates == [{'update_id': 7}]
