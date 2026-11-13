"""One packer per source at a time — a lock file that names its holder.

The lock is `<output_dir>/<source>/.pack.lock`, holding the holder's pid and its process creation
time. The pid alone is not an identity — Windows reuses pids quickly — so a lock counts as held only
while a process with that pid AND that creation time is alive. A lock left by a dead process (a
killed run, a host reboot) is taken over; the run that finds it says so.

**Taken in one atomic step.** The complete record is written to a temporary file first and then linked
under the lock's name with `os.link`, which fails when the name exists — two packers starting together
cannot both win, and nobody ever reads a half-written lock. (Creating the name with `O_EXCL` and writing
it afterwards would leave a moment in which the lock is empty and reads as stale, so a second packer
would take it over. The collector found that in our first version, 2026-11-13; both locks now link.)
"""
import json
import os
import uuid
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

import psutil

LOCK_FILENAME = '.pack.lock'
_SAME_PROCESS_TOLERANCE_S = 0.01


class PackLockHeld(Exception):
    """Another live process holds the source's pack lock."""


class PackLock:
    """`with PackLock(folder):` — refuses with `PackLockHeld` while another live packer holds it."""

    def __init__(self, folder: Path) -> None:
        self._path = folder / LOCK_FILENAME
        self._held = False
        self.took_over: Optional[str] = None     # the dead holder's pid, when a stale lock was taken

    def __enter__(self) -> 'PackLock':
        self._path.parent.mkdir(parents=True, exist_ok=True)
        me = psutil.Process()
        temp = self._path.parent / f'{LOCK_FILENAME}.{me.pid}.{uuid.uuid4().hex}.tmp'
        with open(temp, 'w', encoding='utf-8') as handle:
            json.dump({'pid': me.pid, 'create_time': me.create_time()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            for _ in range(2):
                try:
                    os.link(temp, self._path)      # the complete record appears at once, or not at all
                except FileExistsError:
                    holder = _live_holder(self._path)
                    if holder is not None:
                        raise PackLockHeld(f'pack lock held by pid {holder} ({self._path})') from None
                    # Stale: its holder is gone. Remove it and try once more — a second packer racing for
                    # the same stale lock loses at the link, which is the point.
                    self.took_over = _read_pid(self._path)
                    try:
                        self._path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                self._held = True
                return self
        finally:
            temp.unlink(missing_ok=True)
        raise PackLockHeld(f'pack lock could not be taken ({self._path})')

    def __exit__(self, exc_type: Optional[Type[BaseException]], exc: Optional[BaseException],
                 traceback: Optional[TracebackType]) -> None:
        if self._held:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
            self._held = False


def _live_holder(path: Path) -> Optional[int]:
    """The holder's pid if that very process is still alive; None for a stale or unreadable lock."""
    try:
        holder = json.loads(path.read_text(encoding='utf-8'))
        pid, created = int(holder['pid']), float(holder['create_time'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        process = psutil.Process(pid)
        alive_and_same = abs(process.create_time() - created) <= _SAME_PROCESS_TOLERANCE_S
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return pid if alive_and_same else None


def _read_pid(path: Path) -> Optional[str]:
    try:
        return str(json.loads(path.read_text(encoding='utf-8')).get('pid'))
    except (OSError, ValueError, AttributeError):
        return None
