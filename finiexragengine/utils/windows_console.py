"""Windows console hardening for the live display (ISSUE_26).

The legacy Windows console (conhost — behind both cmd and PowerShell windows) ships with
**QuickEdit Mode** on: a stray click or keypress puts the console into a selection/pause state
that **blocks the process's next stdout write** until a key is pressed. The live dashboard writes
from the event-loop render task, so a blocked write freezes the whole loop (the workers with it) —
the engine looks "hung" until you hit a key. Clearing QuickEdit removes that accidental pause.

Pure + dependency-free (stdlib only). A no-op on non-Windows and when stdout is not a real
console (piped / redirected).
"""
import sys


def disable_quickedit() -> None:
    """Clear ENABLE_QUICK_EDIT_MODE on the Windows console input handle; no-op elsewhere."""
    if sys.platform != 'win32':
        return
    import ctypes
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
