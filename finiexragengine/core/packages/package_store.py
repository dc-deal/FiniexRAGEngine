"""The package folders — what a package is on disk, and the only code that moves it.

    <output_dir>/<source>/ready/<id>.zip             a package nobody has confirmed yet
    <output_dir>/<source>/ready/<id>.manifest.json   its manifest — THE commit point
    <output_dir>/<source>/ready/<id>.receipt.json    a verified receipt, until the package moves
    <output_dir>/<source>/delivered/…                the same three, once delivered
    <output_dir>/<source>/last_run.json              the last run's outcome for this source
    <output_dir>/<source>/.pack.lock                 the packer's lock (`pack_lock`)

**A package exists from the moment its manifest exists.** The zip is written as `.part`, renamed,
then the manifest is written atomically; a zip without a manifest is a run that died, and the next
run removes it. A delivered package stays listed and its bytes never change.

**Receipts: the first verified one wins.** It is written to a unique temp file and linked into place
with `os.link`, which fails when a receipt already exists — two consumers confirming at once cannot
both win. Only a receipt carrying the zip's hash reaches the slot: one without is refused before it
(`package_router`, 400, booked as `receipt_refused`), so it can never block the real one later — the
collector's store once let a hashless first receipt take the slot for good.

Ported from the collector's package service (FiniexDataCollector `python/packages/package_store.py`,
MIT), reduced to what a package rebuilt from a journal needs: there are no originals to stage.
"""
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from finiexragengine.types.package_types import PACKAGE_ID, FoundPackage

READY = 'ready'
DELIVERED = 'delivered'
ZIP_SUFFIX = '.zip'
MANIFEST_SUFFIX = '.manifest.json'
RECEIPT_SUFFIX = '.receipt.json'
PART_SUFFIX = '.part'
LAST_RUN_FILENAME = 'last_run.json'


class PackageStore:
    """The packages under one output folder, grouped by source."""

    def __init__(self, root: Path) -> None:
        self._root = root
        # Manifests are read on every list request; they never change once written, so each is
        # parsed once and kept while its file's size and mtime stay the same.
        self._manifest_cache: Dict[Path, Tuple[Tuple[int, int], Dict[str, Any]]] = {}

    @property
    def root(self) -> Path:
        return self._root

    def source_dir(self, source: str) -> Path:
        return self._root / source

    def ready_dir(self, source: str) -> Path:
        return self._root / source / READY

    def delivered_dir(self, source: str) -> Path:
        return self._root / source / DELIVERED

    def sources(self) -> List[str]:
        """Every source with a folder of its own, sorted."""
        if not self._root.is_dir():
            return []
        return sorted(entry.name for entry in self._root.iterdir()
                      if entry.is_dir() and not entry.name.startswith('.'))

    def manifests(self, source: str) -> List[Dict[str, Any]]:
        """Every committed package of one source, oldest (lowest sequence) first."""
        found: List[Dict[str, Any]] = []
        for folder in (self.ready_dir(source), self.delivered_dir(source)):
            if not folder.is_dir():
                continue
            for path in folder.glob(f'*{MANIFEST_SUFFIX}'):
                manifest = self._read_manifest(path)
                if manifest is not None:
                    found.append(manifest)
        return sorted(found, key=lambda manifest: manifest['sequence'])

    def all_manifests(self) -> List[Dict[str, Any]]:
        """Every committed package of every source, oldest first (by creation, then id)."""
        every = [manifest for source in self.sources() for manifest in self.manifests(source)]
        return sorted(every, key=lambda manifest: (manifest['created_at'], manifest['package_id']))

    def packed_names(self, source: str) -> Dict[str, str]:
        """Member name → the package that holds it, for one source. A name is packed once, ever."""
        return {member['name']: manifest['package_id']
                for manifest in self.manifests(source) for member in manifest['members']}

    def find(self, package_id: str) -> Optional[FoundPackage]:
        """One committed package by id, or None — also for an id that is not an id at all."""
        match = PACKAGE_ID.match(package_id)
        if match is None:
            return None
        source = match.group('source')
        for state, folder in ((READY, self.ready_dir(source)), (DELIVERED, self.delivered_dir(source))):
            manifest_path = folder / f'{package_id}{MANIFEST_SUFFIX}'
            manifest = self._read_manifest(manifest_path)
            if manifest is None:
                continue
            return FoundPackage(package_id=package_id, source=source, state=state, manifest=manifest,
                                zip_path=folder / f'{package_id}{ZIP_SUFFIX}',
                                receipt=_read_json(folder / f'{package_id}{RECEIPT_SUFFIX}'))
        return None

    def next_id(self, source: str, cutoff: datetime) -> Tuple[str, int, Optional[Dict[str, Any]]]:
        """The next package's id and sequence, and the previous package's manifest (None for the first)."""
        existing = self.manifests(source)
        previous = existing[-1] if existing else None
        sequence = previous['sequence'] + 1 if previous else 1
        return f'{source}-{sequence:04d}-{cutoff:%Y%m%d}', sequence, previous

    def clean_incomplete(self, source: str) -> List[str]:
        """Remove what a dead run left in `ready/`: `.part` files, and zips without a manifest."""
        removed: List[str] = []
        folder = self.ready_dir(source)
        if not folder.is_dir():
            return removed
        for path in sorted(folder.iterdir()):
            orphan_zip = (path.name.endswith(ZIP_SUFFIX)
                          and not (folder / (path.name[:-len(ZIP_SUFFIX)] + MANIFEST_SUFFIX)).exists())
            if path.name.endswith(PART_SUFFIX) or orphan_zip:
                path.unlink(missing_ok=True)
                removed.append(path.name)
        return removed

    def write_receipt(self, package_id: str, receipt: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Record the first verified receipt: `(receipt, True)`, or `(the existing one, False)`.
        `(None, False)` when no such package exists."""
        found = self.find(package_id)
        if found is None:
            return None, False
        if found.receipt is not None:
            return found.receipt, False
        folder = found.zip_path.parent
        target = folder / f'{package_id}{RECEIPT_SUFFIX}'
        # `.tmp`, not `.part`: a pack run starting this instant cleans `.part` files from `ready/`.
        temp = folder / f'.{package_id}.{uuid.uuid4().hex}.receipt.tmp'
        try:
            with open(temp, 'w', encoding='utf-8') as handle:
                json.dump(receipt, handle, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, target)          # fails if a receipt is already there: first one wins
            except FileExistsError:
                return _read_json(target), False
        finally:
            temp.unlink(missing_ok=True)
        return receipt, True

    def deliver(self, package_id: str) -> bool:
        """Move a package from `ready/` to `delivered/`: zip, receipt, the manifest last — so an
        interrupted move leaves it findable, and repeating the move finishes it. True if anything moved."""
        match = PACKAGE_ID.match(package_id)
        if match is None:
            return False
        source = match.group('source')
        ready, delivered = self.ready_dir(source), self.delivered_dir(source)
        delivered.mkdir(parents=True, exist_ok=True)
        moved = False
        for suffix in (ZIP_SUFFIX, RECEIPT_SUFFIX, MANIFEST_SUFFIX):
            origin = ready / f'{package_id}{suffix}'
            if origin.exists():
                os.replace(origin, delivered / origin.name)
                moved = True
        return moved

    def write_last_run(self, source: str, record: Dict[str, Any]) -> None:
        write_json_atomically(self.source_dir(source) / LAST_RUN_FILENAME, record)

    def last_runs(self) -> Dict[str, Dict[str, Any]]:
        """Each source's last run record."""
        runs: Dict[str, Dict[str, Any]] = {}
        for source in self.sources():
            record = _read_json(self.source_dir(source) / LAST_RUN_FILENAME)
            if record is not None:
                runs[source] = record
        return runs

    def _read_manifest(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._manifest_cache.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        manifest = _read_json(path)
        if manifest is not None:
            self._manifest_cache[path] = (key, manifest)
        return manifest


def write_json_atomically(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON so a reader sees the old file or the new one, never half of one: a temp file in the
    same folder, fsync, then `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f'.{path.name}.{os.getpid()}{PART_SUFFIX}'
    with open(temp, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=1, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            value = json.load(handle)
    except (FileNotFoundError, ValueError):
        return None
    return value if isinstance(value, dict) else None
