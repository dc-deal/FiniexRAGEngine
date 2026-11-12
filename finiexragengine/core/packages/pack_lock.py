"""One packer per source at a time — a lock file that names its holder.

The lock is `<output_dir>/<source>/.pack.lock`, holding the holder's pid and its process creation
time. The pid alone is not an identity — Windows reuses pids quickly — so a lock counts as held only
while a process with that pid AND that creation time is alive. A lock left by a dead process (a
killed run, a host reboot) is taken over; the run that finds it says so.

Created with `O_EXCL`, so two packers starting together cannot both pass a check-then-write: one
creates the file, the other finds it. (The collector's lock wrote without `O_EXCL` and left that
window open; ours closes it.)
"""
import json
import os
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
        for _ in range(2):
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = _live_holder(self._path)
                if holder is not None:
                    raise PackLockHeld(f'pack lock held by pid {holder} ({self._path})')
                # Stale: its holder is gone. Remove it and try once more — a second packer racing for
                # the same stale lock loses at O_EXCL, which is the point.
                self.took_over = _read_pid(self._path)
                try:
                    self._path.unlink()
                except FileNotFoundError:
                    pass
                continue
            me = psutil.Process()
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump({'pid': me.pid, 'create_time': me.create_time()}, handle)
            self._held = True
            return self
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
