"""Console control-event handling — make a service manager's stop actually reach us (ISSUE_126).

NSSM stops a Windows service with `AppStopMethodConsole`: it detaches from its own console, attaches
to the service's, and raises a console Ctrl+C there. That is the entire stop mechanism, and it can be
silently inert.

**Ctrl+C processing can be switched off in a process, and the setting is inherited by every process
it launches.** A shell that has it off hands it to the engine; the console then *accepts* the event,
the handler never runs, and nothing is logged anywhere — so the service manager waits out its timeout
and escalates to `TerminateProcess`, which kills the pass in flight and skips the ordered shutdown in
`api_app.lifespan`. The FiniexDataCollector reproduced exactly this against their real process on
2026-09-21: the event was accepted and the process ran on for a full minute. With the call below, the
same test exited in 0.15 s with code 0.

Note the argument. `SetConsoleCtrlHandler(NULL, FALSE)` **removes** the ignore state;
`SetConsoleCtrlHandler(NULL, TRUE)` installs it, is a valid call, returns success, and would make
this process permanently unstoppable by the mechanism its service manager uses to stop it. One
character apart, so `tests/cli/test_server_cli.py` pins which one is called.

A process started by the Service Control Manager *should* inherit nothing. This call is what removes
the word "should" from that sentence, for the cost of one line.
"""
import ctypes
import sys


def restore_console_ctrl_handling() -> None:
    """Undo an inherited "ignore Ctrl+C", so a console stop event reaches this process.

    Call once at the top of a long-lived CLI's `main()`, before any signal handler is registered —
    `uvicorn.run` installs its own. A no-op off Windows: there the mechanism does not exist and a
    service manager sends SIGTERM, which nothing can inherit away.
    """
    if sys.platform != 'win32':
        return
    # Add=False REMOVES the ignore state — see the module docstring on the inverted call.
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
