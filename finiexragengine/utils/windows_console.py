"""Windows console hardening — two different couplings between a console and this process.

Both are `kernel32` calls guarded by `sys.platform`, both no-ops elsewhere, both stdlib-only. They
are kept together because "what do we do to the Windows console" should have one answer, and apart
in the docstring because they protect different things at different times.

**QuickEdit (ISSUE_26) protects our writes.** The legacy Windows console (conhost — behind both cmd
and PowerShell) ships with QuickEdit Mode on: a stray click or keypress puts the console into a
selection/pause state that **blocks the process's next stdout write** until a key is pressed. The
live dashboard writes from the event-loop render task, so a blocked write freezes the whole loop,
the workers with it, and the engine looks hung until somebody hits a key. The FiniexDataCollector
measured a 24-second stall from exactly this on 2026-09-18.

**Control handling (ISSUE_126) protects our stop.** NSSM stops a service by attaching to its console
and raising a Ctrl+C there — that is the whole stop mechanism. Ctrl+C processing can be switched off
in a process and is **inherited by every process it launches**, and when it is, the console *accepts*
the event, no handler runs, nothing is logged, and the service manager escalates to
`TerminateProcess`. The collector reproduced it against their real process: it ran on for a full
minute; with the call below, 0.15 s and exit code 0.

Note the argument on that second one. `SetConsoleCtrlHandler(NULL, FALSE)` **removes** the ignore
state; `SetConsoleCtrlHandler(NULL, TRUE)` installs it, is a valid call, returns success, and would
leave a process its own service manager can no longer stop. One character apart, so
`tests/cli/test_server_cli.py` pins which one is called.
"""
import ctypes
import sys


def disable_quickedit() -> None:
    """Clear ENABLE_QUICK_EDIT_MODE on the Windows console input handle; no-op elsewhere.

    Call from whatever owns the terminal, before it starts drawing.
    """
    if sys.platform != 'win32':
        return
    # Local, and load-bearing: `ctypes.wintypes` raises on import off Windows, so it cannot join the
    # module-level import the way plain `ctypes` can.
    from ctypes import wintypes

    std_input_handle = -10
    enable_extended_flags = 0x0080
    enable_quick_edit_mode = 0x0040

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(std_input_handle)
    mode = wintypes.DWORD()
    # GetConsoleMode fails when the handle is not a console (piped/redirected) — nothing to harden.
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return
    # The extended-flags bit must be set for the quick-edit bit to take effect; clear quick-edit so
    # a click/keypress can no longer pause the console (and thereby block our stdout writes).
    new_mode = (mode.value | enable_extended_flags) & ~enable_quick_edit_mode
    kernel32.SetConsoleMode(handle, new_mode)


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
