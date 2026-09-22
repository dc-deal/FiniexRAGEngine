"""`server_cli` — parameter reception only, but four behaviours are worth pinning.

The bind default and the configuration-error path are ISSUE_98; the exit code and the
console control call are ISSUE_126, and both exist for a service manager rather than for a
human at a terminal.
"""
import sys

import pytest

from finiexragengine.cli import server_cli
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.utils import console_ctrl


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

    # 2, not 1, and the difference is addressed to a service manager (ISSUE_126): NSSM restarts on
    # exit by default, which is right for a crash and useless for a configuration refusal. Neither a
    # schema behind, a missing instance identity nor a bad token improves by being retried, so the
    # service definition maps 2 to "final" — and 1 stays "crashed, try again".
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert 'no consumer tokens are configured' in err
    assert 'Traceback' not in err


def test_an_inherited_ignored_ctrl_c_is_removed_on_windows(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The one call that makes a service manager's stop reach this process (ISSUE_126).

    NSSM stops a service by raising a console Ctrl+C. That processing can be switched off in a
    process and is INHERITED by everything it launches — the console then accepts the event, no
    handler runs, nothing is logged, and the manager escalates to `TerminateProcess`.

    The argument is the whole test. `Add=False` REMOVES the ignore state; `Add=True` installs it,
    is a valid call, returns success, and would leave a process that its own service manager can
    no longer stop.
    """
    calls = []

    class _FakeKernel32:
        def SetConsoleCtrlHandler(self, handler: object, add: bool) -> int:
            calls.append((handler, add))
            return 1

    class _FakeCtypes:
        windll = type('_Windll', (), {'kernel32': _FakeKernel32()})()

    monkeypatch.setattr(console_ctrl, 'ctypes', _FakeCtypes)
    monkeypatch.setattr(console_ctrl.sys, 'platform', 'win32')

    console_ctrl.restore_console_ctrl_handling()

    assert calls == [(None, False)]


def test_it_is_a_no_op_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ctypes.windll` does not exist on Linux, and a service manager there sends SIGTERM.

    Asserted rather than assumed because the suite runs on Linux: an unguarded call would fail here
    on import of the attribute, and a guard that silently stopped working on Windows would be
    invisible — so the platform branch is pinned from both sides.
    """
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError('no Windows API call may happen off win32')

    monkeypatch.setattr(console_ctrl, 'ctypes',
                        type('_Boom', (), {'windll': property(explode)})())
    monkeypatch.setattr(console_ctrl.sys, 'platform', 'linux')

    console_ctrl.restore_console_ctrl_handling()      # no raise = no call
