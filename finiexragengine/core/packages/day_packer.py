"""One package run for one source — closed UTC days, straight from the journal, into one zip.

A source is a pipeline. A run takes every closed UTC day before the cutoff that holds rows and that no
earlier package of this source names, reads each day on its own from the journal, and writes it as
one member `<pipeline_id>_<YYYY-MM-DD>.jsonl`. The bytes are the export file of that day exactly:
the same bucket read (`OutcomeArchiveExporter.read_window`) and the same rendering (`archive_text`).

Why the journal and not the export folder: the collector packs files that are its only copy, so it
stages originals, proves them again before deleting, and guards an open-file pattern. Our files are
a rendering of the journal and can be rebuilt at any time, so none of that is needed — and a peek
export of a still-open day (`export_cli --include-open`) can never reach a package.

The steps, each failing loudly:
1. refuse a cutoff that is not a closed UTC midnight, or a start inside the quiet minutes;
2. take the source's `PackLock`; clean what a dead run left; book any committed package the ledger
   lacks;
3. refuse without a mint (the journal's instance identity), a folder that cannot be written, or a
   disk below `min_free_percent`;
4. select the days, write `<id>.zip.part` one day at a time (one day in memory, never the history);
5. read every member back out of the finished zip and compare names and SHA-256 — not only the CRC;
6. fsync, rename to `<id>.zip`, write the manifest atomically — **the commit point**;
7. book `packed` in the ledger, after the commit and never before; write `last_run.json`.

Ported in its steps from the collector's packer (FiniexDataCollector `python/packages/packer.py`, MIT).
"""
import hashlib
import logging
import os
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg

from finiexragengine.core.outcome.outcome_exporter import OutcomeArchiveExporter, archive_text
from finiexragengine.core.packages.pack_lock import PackLock, PackLockHeld
from finiexragengine.core.packages.package_ledger import PackageLedger, packed_fields
from finiexragengine.core.packages.package_store import (
    MANIFEST_SUFFIX,
    PART_SUFFIX,
    ZIP_SUFFIX,
    PackageStore,
    write_json_atomically,
)
from finiexragengine.core.packages.span_records import outages, process_starts
from finiexragengine.types.package_types import (
    MANIFEST_SCHEMA,
    PRODUCER,
    SourceRun,
    member_name,
)
from finiexragengine.utils.quiet_minutes import in_quiet_minutes

logger = logging.getLogger(__name__)

COMPRESS_LEVEL = 6


@dataclass
class PackContext:
    """What a package run needs from outside: where packages live, the journal, and who packs."""
    store: PackageStore
    ledger: PackageLedger
    exporter: OutcomeArchiveExporter
    database_url: str
    mint: Optional[str]                 # the journal's instance identity; no mint, no package
    packer: Dict[str, Any]              # {program, version, commit, dirty}
    min_free_percent: float


def default_cutoff(now: datetime) -> datetime:
    """The UTC midnight at or before `now` — what a run packs up to when no cutoff is named."""
    utc = now.astimezone(timezone.utc)
    return utc.replace(hour=0, minute=0, second=0, microsecond=0)


def pack_source(source: str, context: PackContext, cutoff: datetime,
                now: Optional[datetime] = None) -> SourceRun:
    """Pack one source to `cutoff`. Never raises: every outcome is a `SourceRun`, also written to
    the source's `last_run.json`."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started = time.monotonic()
    refusal = _cutoff_refusal(cutoff, now)
    if refusal is not None:
        return _finish(context, source, cutoff, started, 'refused', reason=refusal)
    try:
        with PackLock(context.store.source_dir(source)) as lock:
            if lock.took_over is not None:
                logger.warning('[PACK] %s: took over a stale pack lock (pid %s)', source, lock.took_over)
            return _pack_locked(source, context, cutoff, now, started)
    except PackLockHeld as exc:
        return _finish(context, source, cutoff, started, 'refused', reason=str(exc))
    except Exception as exc:    # a run never takes its caller down; the reason is recorded
        logger.exception('[PACK] %s: run failed', source)
        return _finish(context, source, cutoff, started, 'failed', reason=f'{type(exc).__name__}: {exc}')


def _pack_locked(source: str, context: PackContext, cutoff: datetime, now: datetime,
                 started: float) -> SourceRun:
    store = context.store
    removed = store.clean_incomplete(source)
    entered = context.ledger.seed(store.manifests(source))
    extra = {'removed_incomplete': removed, 'entered_late': entered}
    if not context.mint:
        return _finish(context, source, cutoff, started, 'refused', **extra,
                       reason='no mint: the journal carries no instance identity (migration 017)')
    ready = store.ready_dir(source)
    ready.mkdir(parents=True, exist_ok=True)
    probe = _probe_write(ready)
    if probe is not None:
        return _finish(context, source, cutoff, started, 'refused', reason=probe, **extra)
    free_percent = _free_percent(ready)
    if free_percent < context.min_free_percent:
        return _finish(context, source, cutoff, started, 'refused', **extra,
                       reason=f'disk_low: {free_percent:.1f} % free < {context.min_free_percent:g} %')

    cutoff_day = f'{cutoff:%Y-%m-%d}'
    packed = store.packed_names(source)
    days = [entry.day for entry in context.exporter.day_counts(source)
            if entry.lines > 0 and entry.day < cutoff_day and member_name(source, entry.day) not in packed]
    if not days:
        return _finish(context, source, cutoff, started, 'nothing_new', **extra)

    package_id, sequence, previous = store.next_id(source, cutoff)
    zip_part = ready / f'{package_id}{ZIP_SUFFIX}{PART_SUFFIX}'
    zip_path = ready / f'{package_id}{ZIP_SUFFIX}'
    try:
        members, excluded = _write_zip(zip_part, source, days, context)
        if not members:
            zip_part.unlink(missing_ok=True)
            return _finish(context, source, cutoff, started, 'nothing_new', excluded=excluded, **extra,
                           reason='every new day was excluded')
        _self_check(zip_part, members)
        _fsync(zip_part)
        zip_sha256 = _sha256_of(zip_part)
        os.replace(zip_part, zip_path)
        span_from = (datetime.fromisoformat(previous['cutoff']) if previous is not None
                     else datetime.fromisoformat(f'{days[0]}T00:00:00+00:00'))
        manifest = build_manifest(
            source=source, package_id=package_id, sequence=sequence,
            previous_package_id=previous['package_id'] if previous is not None else None,
            cutoff=cutoff, created_at=now, mint=context.mint,
            zip_info={'name': zip_path.name, 'size_bytes': zip_path.stat().st_size, 'sha256': zip_sha256},
            members=members, excluded=excluded, span_from=span_from,
            outages=_or_unknown(lambda: outages(context.database_url, span_from, cutoff, now)),
            starts=_or_unknown(lambda: process_starts(context.database_url, span_from, cutoff)),
            packer=context.packer)
        write_json_atomically(ready / f'{package_id}{MANIFEST_SUFFIX}', manifest)   # the commit
    except BaseException:
        zip_part.unlink(missing_ok=True)
        raise
    context.ledger.append('packed', **packed_fields(manifest))
    return _finish(context, source, cutoff, started, 'packed', package_id=package_id,
                   member_count=manifest['member_count'], record_count=manifest['record_count'],
                   zip_size_bytes=manifest['zip']['size_bytes'], excluded=excluded, **extra)


def _write_zip(zip_part: Path, source: str, days: List[str], context: PackContext
               ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Write the selected days as members, one day in memory at a time. A day whose lines name
    another producer than this journal's mint is excluded with both ids, never packed."""
    members: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    with zipfile.ZipFile(zip_part, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=COMPRESS_LEVEL, allowZip64=True) as archive:
        for day in days:
            start = datetime.fromisoformat(f'{day}T00:00:00+00:00')
            lines = context.exporter.read_window(source, start, start + timedelta(days=1))
            name = member_name(source, day)
            foreign = sorted({str(line['instance_id']) for line in lines
                              if line.get('instance_id') and line['instance_id'] != context.mint})
            if foreign:
                excluded.append({'name': name, 'reason': f'instance_id {", ".join(foreign)} is not '
                                                         f"this journal's mint {context.mint}"})
                continue
            data = archive_text(lines).encode('utf-8')
            info = zipfile.ZipInfo(name, date_time=_member_time(lines, start))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compresslevel=COMPRESS_LEVEL)
            members.append(describe_member(name, data, lines))
    return members, excluded


def describe_member(name: str, data: bytes, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A member's manifest entry: the contract's `name`, `size_bytes`, `sha256`, then ours."""
    fingerprints: List[str] = []
    for line in lines:
        value = line.get('config_fingerprint')
        if value and value not in fingerprints:
            fingerprints.append(value)
    return {'name': name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'record_count': len(lines),
            'first_collected_msc': lines[0].get('collected_msc') if lines else None,
            'last_collected_msc': lines[-1].get('collected_msc') if lines else None,
            'config_fingerprints': fingerprints}


def build_manifest(*, source: str, package_id: str, sequence: int, previous_package_id: Optional[str],
                   cutoff: datetime, created_at: datetime, mint: str, zip_info: Dict[str, Any],
                   members: List[Dict[str, Any]], excluded: List[Dict[str, str]], span_from: datetime,
                   outages: Optional[Dict[str, Any]], starts: Optional[Dict[str, Any]],
                   packer: Dict[str, Any]) -> Dict[str, Any]:
    """THE manifest. Its top-level keys are the contract shared with the collector's tick packages;
    `held_for_next` and `open_files` are always empty here (whole days, and a running day has no
    file), and kept so one client reads both services."""
    return {
        'schema': MANIFEST_SCHEMA,
        'package_id': package_id,
        'sequence': sequence,
        'source': source,
        'instance_id': mint,
        'producer': PRODUCER,
        'cutoff': cutoff.isoformat(),
        'previous_package_id': previous_package_id,
        'created_at': created_at.replace(microsecond=0).isoformat(),
        'zip': {**zip_info, 'compression': 'deflate'},
        'member_count': len(members),
        'record_count': sum(member['record_count'] for member in members),
        'span': {'from': span_from.isoformat(), 'until': cutoff.isoformat()},
        'members': members,
        'excluded': excluded,
        'held_for_next': [],
        'open_files': [],
        'outages': outages,
        'process_starts': starts,
        'packer': packer,
    }


def _self_check(zip_part: Path, members: List[Dict[str, Any]]) -> None:
    """Every member read back out of the written zip: the same names, the same SHA-256 each."""
    expected = {member['name']: member['sha256'] for member in members}
    with zipfile.ZipFile(zip_part) as archive:
        if sorted(archive.namelist()) != sorted(expected):
            raise OSError('the written zip does not hold exactly the packed days')
        for info in archive.infolist():
            if hashlib.sha256(archive.read(info)).hexdigest() != expected[info.filename]:
                raise OSError(f'the written zip gives back other bytes for {info.filename}')


def _cutoff_refusal(cutoff: datetime, now: datetime) -> Optional[str]:
    if cutoff.tzinfo is None:
        return 'the cutoff has no time zone (UTC midnight expected)'
    utc = cutoff.astimezone(timezone.utc)
    if utc != default_cutoff(utc):
        return f'the cutoff {utc.isoformat()} is not a UTC midnight'
    if utc > now:
        return f'the cutoff {utc:%Y-%m-%d} has not passed yet: the day before it is still open'
    if in_quiet_minutes(now):
        return 'inside the quiet minutes 23:55-00:15 UTC — no package run starts there'
    return None


def _member_time(lines: List[Dict[str, Any]], day_start: datetime) -> Tuple[int, int, int, int, int, int]:
    """A member's date in the zip: its day's last line, so a repack gives the same entry."""
    last = lines[-1].get('collected_msc') if lines else None
    moment = (datetime.fromtimestamp(last / 1000, tz=timezone.utc) if isinstance(last, (int, float))
              else day_start)
    return moment.timetuple()[:6]


def _probe_write(folder: Path) -> Optional[str]:
    """Write, flush, rename, read back and delete a probe — a folder that refuses is found before a
    zip is written, not in the middle of one."""
    probe = folder / f'.probe.{os.getpid()}.tmp'
    renamed = folder / f'.probe.{os.getpid()}.renamed'
    try:
        with open(probe, 'wb') as handle:
            handle.write(b'probe')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(probe, renamed)
        if renamed.read_bytes() != b'probe':
            return f'the package folder {folder} gives back other bytes'
        renamed.unlink()
    except OSError as exc:
        return f'the package folder {folder} cannot be written: {exc}'
    finally:
        probe.unlink(missing_ok=True)
        renamed.unlink(missing_ok=True)
    return None


def _free_percent(folder: Path) -> float:
    usage = shutil.disk_usage(folder)
    return usage.free / usage.total * 100 if usage.total else 0.0


def _or_unknown(read: Any) -> Optional[Dict[str, Any]]:
    """A span record, or None ("not known") when its source cannot be read — never a failed run."""
    try:
        return read()
    except psycopg.Error as exc:
        logger.warning('[PACK] span record unavailable: %s', exc)
        return None


def _fsync(path: Path) -> None:
    with open(path, 'rb+') as handle:
        os.fsync(handle.fileno())


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _finish(context: PackContext, source: str, cutoff: datetime, started: float, result: str,
            **fields: Any) -> SourceRun:
    run = SourceRun(source=source, result=result, at=datetime.now(timezone.utc).isoformat(),
                    cutoff=cutoff.isoformat() if cutoff.tzinfo else str(cutoff),
                    duration_seconds=round(time.monotonic() - started, 2), **fields)
    try:
        context.store.write_last_run(source, run.to_record())
    except OSError as exc:
        logger.error('[PACK] %s: last_run.json not written: %s', source, exc)
    log = logger.warning if result in ('refused', 'failed') else logger.info
    log('[PACK] %s · %s%s%s', source, result,
        f' · {run.package_id}' if run.package_id else '', f' · {run.reason}' if run.reason else '')
    return run
