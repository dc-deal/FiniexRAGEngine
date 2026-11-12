"""The package ledger — every step of every package, appended, never rewritten.

`<output_dir>/ledger.jsonl` holds one JSON line per event: `packed`, `served` (each download, when
it ends), `received`, `receipt_refused`, `dev_received`, `delivered`, and the schedule's events.
There is no database behind it on purpose: the packages are files, and their record lies beside
them, readable with any text tool.

Two processes write it — the engine (downloads, receipts, the schedule) and the package run it
starts (`packed`) — so each append holds an OS lock on `.ledger.lock` and is fsynced. A torn last
line (a process killed mid-write) is counted as unreadable when read, and the next append starts
a line of its own rather than gluing itself onto the fragment.

**A record, not a gate.** A ledger that cannot be written stops no package and no download; the
error goes to the log. The one decision that reads it — whether a receipt's token downloaded the
whole zip — then refuses, which is the safe side.

Ported from the collector's package service (FiniexDataCollector `python/packages/ledger.py`,
MIT), so `downloaded_whole` decides exactly as theirs does: one contract, one rule.
"""
import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from finiexragengine.types.package_types import LedgerRead

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

LEDGER_FILENAME = 'ledger.jsonl'
LOCK_FILENAME = '.ledger.lock'


class PackageLedger:
    """Append and read `ledger.jsonl` under `root` (the packages' output folder)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / LEDGER_FILENAME
        self._lock_path = root / LOCK_FILENAME
        self._thread_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: str, **fields: Any) -> None:
        """Book one event. Never raises: a ledger that cannot be written is logged, not fatal."""
        record = {'event': event, 'at': _now_iso_ms(), **fields}
        line = json.dumps(record, ensure_ascii=False) + '\n'
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with self._thread_lock, _os_lock(self._lock_path):
                with open(self._path, 'a+b') as handle:
                    # A torn last line (a writer killed mid-line) must not swallow this event.
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() > 0:
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) != b'\n':
                            handle.write(b'\n')
                    handle.write(line.encode('utf-8'))
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            logger.error('[PACKAGES] ledger append failed (%s, event %s): %s', self._path, event, exc)

    def read(self, package_id: Optional[str] = None, event: Optional[str] = None) -> LedgerRead:
        """Every event, oldest first, optionally narrowed to one package and/or one event name."""
        events: List[Dict[str, Any]] = []
        unreadable = 0
        try:
            with open(self._path, 'r', encoding='utf-8') as handle:
                for raw in handle:
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        unreadable += 1
                        continue
                    if not isinstance(record, dict):
                        unreadable += 1
                        continue
                    if package_id is not None and record.get('package_id') != package_id:
                        continue
                    if event is not None and record.get('event') != event:
                        continue
                    events.append(record)
        except FileNotFoundError:
            pass
        return LedgerRead(count=len(events), unreadable_lines=unreadable, events=events)

    def seed(self, manifests: List[Dict[str, Any]]) -> List[str]:
        """Book `packed` for every committed package the ledger lacks — a run that died between its
        commit (the manifest) and its booking leaves the ledger short, and the next run repairs it."""
        booked = {record.get('package_id') for record in self.read(event='packed').events}
        entered: List[str] = []
        for manifest in manifests:
            if manifest.get('package_id') not in booked:
                self.append('packed', entered_late=True, **packed_fields(manifest))
                entered.append(manifest['package_id'])
        return entered


def packed_fields(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """What a `packed` event records of a manifest: the zip and every member's name and hash."""
    return {
        'package_id': manifest['package_id'],
        'source': manifest['source'],
        'instance_id': manifest.get('instance_id'),
        'cutoff': manifest['cutoff'],
        'created_at': manifest['created_at'],
        'zip': {key: manifest['zip'][key] for key in ('name', 'size_bytes', 'sha256')},
        'member_count': manifest['member_count'],
        'record_count': manifest['record_count'],
        'members': [{'name': member['name'], 'sha256': member['sha256'],
                     'record_count': member['record_count']} for member in manifest['members']],
        'excluded': [item['name'] for item in manifest.get('excluded', [])],
        'held_for_next': list(manifest.get('held_for_next', [])),
    }


def downloaded_whole(events: List[Dict[str, Any]], consumer: Optional[str], size: int) -> bool:
    """Whether `consumer`'s downloads of a zip of `size` bytes together covered every byte.

    The list publishes each package's hash, so a hash alone proves nothing: a receipt also needs a
    complete download by the same token. A download counts for the span it handed over, from its
    range's start — an abandoned attempt included, which is what lets a resumed download add up.
    The `Range` header's end is ignored; `bytes_sent` decides. (Exactly the collector's rule.)
    """
    spans: List[Tuple[int, int]] = []
    for event in events:
        if event.get('event') != 'served' or event.get('consumer') != consumer:
            continue
        sent = int(event.get('bytes_sent') or 0)
        if event.get('status') == 200:
            spans.append((0, sent))
        elif event.get('status') == 206:
            start = _range_start(event.get('range'), size)
            if start is not None:
                spans.append((start, start + sent))
    reached = 0
    for start, end in sorted(spans):
        if start > reached:
            break
        reached = max(reached, end)
    return size > 0 and reached >= size


def _range_start(header: Optional[str], size: int) -> Optional[int]:
    """The first byte a single-range `Range` header asked for; None for anything else."""
    if not header or not header.startswith('bytes=') or ',' in header:
        return None
    first, _, last = header[len('bytes='):].partition('-')
    try:
        if first == '':
            return max(0, size - int(last))
        return int(first)
    except ValueError:
        return None


@contextmanager
def _os_lock(path: Path) -> Iterator[None]:
    """An exclusive OS lock on `path` for the duration — across processes, not only threads."""
    with open(path, 'a+b') as handle:
        if os.name == 'nt':
            handle.seek(0)
            for attempt in range(6):          # LK_LOCK itself retries for ~10 s: about a minute in all
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    if attempt == 5:
                        raise
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
