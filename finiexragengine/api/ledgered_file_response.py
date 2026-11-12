"""A file response that reports how much of the file it actually handed over.

A receipt for a package is accepted only when the same token downloaded the whole zip (the hash is
public on the list, so it proves nothing alone). That needs every download booked with what it
really sent: `status`, `content_length`, `bytes_sent`, `complete`, `disconnected` — after the last
byte, after a client abort and after an error alike.

The wrapper counts the body bytes passed to the server and watches `receive()` for
`http.disconnect`. It removes the `http.response.pathsend` extension from the scope first: a server
offering it would otherwise be handed a path instead of bytes, and nothing could be counted.
`complete` means "handed to the server", not "proven delivered" — the client's receipt is the proof.

`Range` (206) is Starlette's own `FileResponse` behaviour; this wrapper only counts what it sends.
Ported from the collector's package service (FiniexDataCollector `python/packages/package_api.py`, MIT).
"""
from typing import Any, Callable, Dict, MutableMapping, Optional

import anyio
from starlette.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send

DownloadOutcome = Dict[str, Any]


class LedgeredFileResponse(FileResponse):
    """`FileResponse` that calls `on_finish(outcome)` exactly once when the response ends."""

    def __init__(self, path: Any, *, on_finish: Callable[[DownloadOutcome], None], **kwargs: Any) -> None:
        super().__init__(path, **kwargs)
        self._on_finish = on_finish

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Bytes, never a path: the counting below only sees what passes through `send`.
        extensions: Optional[MutableMapping[str, Any]] = scope.get('extensions')
        if extensions and 'http.response.pathsend' in extensions:
            scope = {**scope, 'extensions': {key: value for key, value in extensions.items()
                                             if key != 'http.response.pathsend'}}
        outcome: DownloadOutcome = {'status': None, 'content_length': None, 'bytes_sent': 0,
                                    'complete': False, 'disconnected': False}

        async def counting_send(message: Message) -> None:
            if message['type'] == 'http.response.start':
                outcome['status'] = message['status']
                for name, value in message.get('headers', []):
                    if name.lower() == b'content-length':
                        outcome['content_length'] = int(value)
            elif message['type'] == 'http.response.body':
                outcome['bytes_sent'] += len(message.get('body', b''))
                outcome['complete'] = not message.get('more_body', False)
            await send(message)

        try:
            async with anyio.create_task_group() as group:
                async def watch_disconnect() -> None:
                    while True:
                        message = await receive()
                        if message['type'] == 'http.disconnect':
                            if not outcome['complete']:
                                outcome['disconnected'] = True
                                group.cancel_scope.cancel()
                            return

                group.start_soon(watch_disconnect)
                await super().__call__(scope, receive, counting_send)
                group.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._on_finish, outcome)
