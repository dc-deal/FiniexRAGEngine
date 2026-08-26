"""`server_cli` — parameter reception only, but two behaviours are worth pinning (ISSUE_98)."""
import sys

import pytest

from finiexragengine.cli import server_cli
from finiexragengine.exceptions.ragengine_errors import ConfigurationError


def test_the_default_bind_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine is reached through the TLS-terminating proxy, never directly.

    Binding wide has to be the deliberate exception (a container, where the port mapping controls
    exposure) rather than the default everyone inherits by saying nothing. Until ISSUE_98 the
    default was `0.0.0.0`, and what kept the port shut was the Windows firewall's shipped default —
    the absence of a decision rather than a decision.
    """
    captured = {}
    monkeypatch.setattr(server_cli.uvicorn, 'run',
                        lambda *a, **kw: captured.update(kw))
    monkeypatch.setattr(sys, 'argv', ['server_cli'])
    server_cli.main()
    assert captured['host'] == '127.0.0.1'


def test_a_configuration_error_reads_as_a_message_not_a_crash(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """`create_app` is a factory uvicorn calls from inside `config.load()`.

    So a `ConfigurationError` raised there surfaces wrapped in uvicorn internals, with the one
    actionable line at the bottom of a twenty-line traceback. The guard stays in `create_app` —
    it protects every entry point — but a human reads the result here.
    """
    def boom(*args: object, **kwargs: object) -> None:
        raise ConfigurationError('api.require_auth is on but no consumer tokens are configured')

    monkeypatch.setattr(server_cli.uvicorn, 'run', boom)
    monkeypatch.setattr(sys, 'argv', ['server_cli'])

    with pytest.raises(SystemExit) as exit_info:
        server_cli.main()

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert 'no consumer tokens are configured' in err
    assert 'Traceback' not in err
