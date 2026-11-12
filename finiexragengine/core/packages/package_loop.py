"""The engine's package clock: it decides when a run is due and starts it as a child process.

Every 30 s the loop asks the schedule what is due — planned special runs, then the weekly run — and
starts `python -m finiexragengine.cli.package_cli pack` for it. Nothing starts inside the quiet
minutes. The run itself never happens in the engine's process:

- **Its memory goes back to the OS when it exits.** A run reads the journal one day at a time, but
  the first package of a pipeline is its whole history, and a long-lived process keeps much of what
  it once allocated (the weekly export showed it: +200 MB, then ~+170 MB for good).
- **It runs at below-normal priority**, set by the child itself (`lower_priority`), so a pass of the
  signal pipelines is never queued behind it.
- **It is started from a thread** (`asyncio.to_thread` around `subprocess.Popen`), not with
  `asyncio.create_subprocess_exec`: that one needs an event loop with subprocess support, which
  uvicorn on Windows does not promise.

A run that outlives `pack_timeout_seconds` is stopped and recorded as failed. A weekly run missed
while the engine was down is not made up (the next one takes everything since); a special run that
was due runs at the next start. Missing the weekly time by a few seconds is not missing it: the next
weekly time is fixed in memory at start and checked on every tick.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from finiexragengine.core.packages.day_packer import default_cutoff
from finiexragengine.core.packages.package_ledger import PackageLedger
from finiexragengine.core.packages.package_schedule import ALL, PackageSchedule
from finiexragengine.utils.quiet_minutes import in_quiet_minutes

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
PACK_MODULE = 'finiexragengine.cli.package_cli'

# (source or None for every source, cutoff) → one run record per source, as `SourceRun.to_record()`.
PackRunner = Callable[[Optional[str], datetime], Awaitable[List[Dict[str, Any]]]]


class PackageLoop:
    """Runs due package runs; `status()` is what the open package health shows of it."""

    def __init__(self, schedule: PackageSchedule, ledger: PackageLedger, runner: PackRunner,
                 on_report: Optional[Callable[[str], Awaitable[None]]] = None,
                 now: Optional[datetime] = None) -> None:
        self._schedule = schedule
        self._ledger = ledger
        self._runner = runner
        self._on_report = on_report
        self._started_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self._weekly_at = schedule.next_weekly(self._started_at)
        self._packing = False
        self._stop = asyncio.Event()

    def status(self) -> Dict[str, Any]:
        special = self._schedule.next_special_at()
        return {'started_at': self._started_at.isoformat(),
                'next_run': self._weekly_at.isoformat(),
                'next_run_set_aside': self._schedule.weekly_skipped(self._weekly_at),
                'next_special': special.isoformat() if special is not None else None,
                'packing': self._packing}

    async def run(self) -> None:
        """Tick until `stop()`; a failing tick is logged and the next one comes."""
        while not self._stop.is_set():
            try:
                await self.step(datetime.now(timezone.utc))
            except Exception:
                logger.exception('[PACKAGES] schedule tick failed')
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def step(self, now: datetime) -> List[str]:
        """Run whatever is due at `now`; returns what ran (`special:<id>`, `weekly`)."""
        if in_quiet_minutes(now):
            return []
        ran: List[str] = []
        for special in self._schedule.due(now):
            source = None if special['source'] == ALL else special['source']
            cutoff = datetime.fromisoformat(special['cutoff'])
            runs = await self._pack(source, cutoff, f"special {special['id']}")
            results = {run['source']: run['result'] for run in runs}
            self._schedule.finish(special['id'], results)
            self._ledger.append('special_ran', special_id=special['id'], source=special['source'],
                                cutoff=special['cutoff'], results=results)
            ran.append(f"special:{special['id']}")
        if now >= self._weekly_at:
            run_at = self._weekly_at
            self._weekly_at = self._schedule.next_weekly(now)
            if self._schedule.weekly_skipped(run_at):
                self._ledger.append('weekly_set_aside', run_at=run_at.isoformat())
                logger.info('[PACKAGES] weekly run %s set aside', run_at.isoformat())
            else:
                await self._pack(None, default_cutoff(run_at), 'weekly')
                ran.append('weekly')
        return ran

    async def _pack(self, source: Optional[str], cutoff: datetime, label: str) -> List[Dict[str, Any]]:
        self._packing = True
        try:
            runs = await self._runner(source, cutoff)
        finally:
            self._packing = False
        text = run_report(label, cutoff, runs)
        logger.info('[PACKAGES] %s', text)
        if self._on_report is not None:
            try:
                await self._on_report(text)
            except Exception:
                logger.exception('[PACKAGES] run report not sent')
        return runs


def run_report(label: str, cutoff: datetime, runs: List[Dict[str, Any]]) -> str:
    """One line per run, for the log and the operator's chat."""
    parts = []
    for run in runs:
        detail = (f"{run.get('member_count', 0)} days · {run.get('record_count', 0)} records · "
                  f"{run.get('zip_size_bytes', 0) / 1e6:.1f} MB" if run.get('result') == 'packed'
                  else run.get('reason') or '')
        parts.append(f"{run.get('source')} {run.get('result')}" + (f' ({detail})' if detail else ''))
    return f'📦 packages · {label} · cutoff {cutoff:%Y-%m-%d} · ' + (' · '.join(parts) or 'no sources')


class PackChildRunner:
    """Runs one package run as a child process and returns its run records.

    The child prints one JSON line on stdout (`{"runs": [...]}`); everything else it says goes to
    stderr and, on failure, into the record's reason. The running child is held so `terminate()` can
    stop it when the engine shuts down — a thread blocked on a child would otherwise hold the
    shutdown for as long as the run takes. A stopped run leaves a `.part` and a lock behind, which
    the next run cleans up and takes over.
    """

    def __init__(self, timeout_seconds: int) -> None:
        self._timeout = timeout_seconds
        self._process: Optional['subprocess.Popen[str]'] = None

    async def __call__(self, source: Optional[str], cutoff: datetime) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self._run, source, cutoff)

    def terminate(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _run(self, source: Optional[str], cutoff: datetime) -> List[Dict[str, Any]]:
        command = [sys.executable, '-m', PACK_MODULE, 'pack', '--source', source or ALL,
                   '--cutoff', f'{cutoff:%Y-%m-%d}', '--json']
        label = source or ALL
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       encoding='utf-8', errors='replace', cwd=os.getcwd(),
                                       env=dict(os.environ))
        except OSError as exc:
            return [_failed(label, cutoff, f'the run could not start: {exc}')]
        self._process = process
        try:
            stdout, stderr = process.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return [_failed(label, cutoff, f'the run did not finish within {self._timeout} s and was stopped')]
        finally:
            self._process = None
        for line in reversed(stdout.splitlines()):
            if line.startswith('{'):
                try:
                    return list(json.loads(line)['runs'])
                except (ValueError, KeyError, TypeError):
                    break
        tail = (stderr or '').strip().splitlines()[-3:]
        return [_failed(label, cutoff, f'exit {process.returncode}: ' + ' | '.join(tail))]


def _failed(source: str, cutoff: datetime, reason: str) -> Dict[str, Any]:
    return {'source': source, 'result': 'failed', 'reason': reason, 'cutoff': cutoff.isoformat(),
            'at': datetime.now(timezone.utc).isoformat()}
