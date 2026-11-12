"""Signal packages — the engine's package clock: what runs when, and the child that does the work.

The runner is a fake here (no child process, no database): these tests pin the decisions. One test
starts a real child to prove the hand-off — that its JSON line is read and a failure becomes a record.
"""
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from finiexragengine.core.packages import package_loop
from finiexragengine.core.packages.package_ledger import PackageLedger
from finiexragengine.core.packages.package_loop import PackageLoop, PackChildRunner, run_report
from finiexragengine.core.packages.package_schedule import PackageSchedule
from finiexragengine.types.config_types.app_config_types import PackagesConfig


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: List[Tuple[Optional[str], datetime]] = []

    async def __call__(self, source: Optional[str], cutoff: datetime) -> List[Dict[str, Any]]:
        self.calls.append((source, cutoff))
        return [{'source': source or 'stream_a', 'result': 'packed', 'member_count': 7,
                 'record_count': 1000, 'zip_size_bytes': 2_000_000}]


def _loop(tmp_path: Path, start: datetime) -> Tuple[PackageLoop, _FakeRunner, PackageSchedule, PackageLedger]:
    schedule = PackageSchedule(tmp_path, PackagesConfig(enabled=True), lambda: ['stream_a', 'stream_b'])
    ledger = PackageLedger(tmp_path)
    runner = _FakeRunner()
    return PackageLoop(schedule, ledger, runner, now=start), runner, schedule, ledger


def test_the_weekly_run_starts_at_its_time_and_packs_to_that_days_midnight(tmp_path):
    loop, runner, _, _ = _loop(tmp_path, _utc(2026, 7, 22, 9, 0))
    assert asyncio.run(loop.step(_utc(2026, 7, 26, 1, 29))) == []
    assert asyncio.run(loop.step(_utc(2026, 7, 26, 1, 30, 20))) == ['weekly']
    assert runner.calls == [(None, _utc(2026, 7, 26))]
    assert asyncio.run(loop.step(_utc(2026, 7, 26, 1, 31))) == []           # once, not every tick
    assert loop.status()['next_run'] == '2026-08-02T01:30:00+00:00'


def test_a_weekly_run_missed_while_down_is_not_made_up(tmp_path):
    """Started after the weekly time, the loop waits for the next one: that run takes everything since."""
    loop, runner, _, _ = _loop(tmp_path, _utc(2026, 7, 26, 9, 0))
    assert asyncio.run(loop.step(_utc(2026, 7, 26, 9, 0, 30))) == []
    assert runner.calls == []


def test_a_set_aside_weekly_run_is_booked_not_run(tmp_path):
    loop, runner, schedule, ledger = _loop(tmp_path, _utc(2026, 7, 22, 9, 0))
    schedule.set_aside(None, _utc(2026, 7, 22, 9, 0), 'operator')
    asyncio.run(loop.step(_utc(2026, 7, 26, 1, 30, 10)))

    assert runner.calls == []
    assert [e['event'] for e in ledger.read().events] == ['weekly_set_aside']


def test_a_due_special_run_runs_once_and_is_marked_done(tmp_path):
    loop, runner, schedule, ledger = _loop(tmp_path, _utc(2026, 7, 22, 9, 0))
    entry = schedule.add('stream_b', date(2026, 7, 23), None, 'operator', _utc(2026, 7, 22, 9, 0))
    assert asyncio.run(loop.step(_utc(2026, 7, 23, 1, 0, 15))) == [f"special:{entry['id']}"]
    assert asyncio.run(loop.step(_utc(2026, 7, 23, 1, 0, 45))) == []

    assert runner.calls == [('stream_b', _utc(2026, 7, 23))]
    assert schedule.due(_utc(2026, 7, 24)) == []
    assert ledger.read(event='special_ran').events[0]['results'] == {'stream_b': 'packed'}


def test_a_special_run_due_while_down_runs_at_the_next_start(tmp_path):
    schedule = PackageSchedule(tmp_path, PackagesConfig(enabled=True), lambda: ['stream_a'])
    schedule.add('stream_a', date(2026, 7, 23), None, 'operator', _utc(2026, 7, 22, 9, 0))
    loop, runner, _, _ = _loop(tmp_path, _utc(2026, 7, 23, 6, 0))            # restarted after its time
    asyncio.run(loop.step(_utc(2026, 7, 23, 6, 0, 30)))
    assert runner.calls == [('stream_a', _utc(2026, 7, 23))]


def test_nothing_starts_inside_the_quiet_minutes(tmp_path):
    loop, runner, schedule, _ = _loop(tmp_path, _utc(2026, 7, 22, 9, 0))
    schedule.add('stream_a', date(2026, 7, 23), None, 'operator', _utc(2026, 7, 22, 9, 0))
    assert asyncio.run(loop.step(_utc(2026, 7, 24, 0, 5))) == []
    assert runner.calls == []


def test_the_run_report_is_one_line_per_source():
    text = run_report('weekly', _utc(2026, 7, 26), [
        {'source': 'stream_a', 'result': 'packed', 'member_count': 7, 'record_count': 1000,
         'zip_size_bytes': 2_000_000},
        {'source': 'stream_b', 'result': 'refused', 'reason': 'disk_low: 4.0 % free < 10 %'}])
    assert text == ('📦 packages · weekly · cutoff 2026-07-26 · stream_a packed (7 days · 1000 records · '
                    '2.0 MB) · stream_b refused (disk_low: 4.0 % free < 10 %)')


def test_the_child_hands_back_its_json_line_and_a_crash_becomes_a_failed_record(monkeypatch, tmp_path):
    """A real child process: what it prints last as JSON is the result; anything else is a failure
    with the end of its stderr as the reason — never an exception into the engine."""
    script = tmp_path / 'child.py'
    script.write_text('import json, sys\n'
                      'print("census line", file=sys.stderr)\n'
                      'if "--json" in sys.argv: print(json.dumps({"runs": [{"source": "stream_a", '
                      '"result": "nothing_new"}]}))\n', encoding='utf-8')
    monkeypatch.setattr(package_loop, 'PACK_MODULE', 'unused')
    monkeypatch.setattr(package_loop.sys, 'executable', sys.executable)
    runner = PackChildRunner(timeout_seconds=60)

    real_popen = package_loop.subprocess.Popen

    def popen(command: List[str], **kwargs: Any) -> Any:
        return real_popen([sys.executable, str(script), *command[3:]], **kwargs)

    monkeypatch.setattr(package_loop.subprocess, 'Popen', popen)
    assert asyncio.run(runner(None, _utc(2026, 7, 26))) == [{'source': 'stream_a', 'result': 'nothing_new'}]

    script.write_text('import sys\nprint("boom: no database", file=sys.stderr)\nsys.exit(2)\n', encoding='utf-8')
    failed = asyncio.run(runner('stream_a', _utc(2026, 7, 26)))
    assert failed[0]['result'] == 'failed' and 'exit 2' in failed[0]['reason']
    assert 'boom: no database' in failed[0]['reason']
