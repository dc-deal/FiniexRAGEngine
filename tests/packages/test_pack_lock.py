"""Signal packages — the pack lock: one packer per source, taken in one atomic step.

The race is run for real: several processes released at the same instant. A check-then-write lock lets
several through; one created empty and written afterwards reads as stale for a moment, and a second
packer takes it over. The collector measured both on its side (2026-11-13).
"""
import json
import multiprocessing
import time
from pathlib import Path
from typing import Any

from finiexragengine.core.packages.pack_lock import LOCK_FILENAME, PackLock, PackLockHeld

_RACERS = 6


def _race(folder: str, barrier: Any, results: Any) -> None:
    barrier.wait()
    try:
        with PackLock(Path(folder)):
            results.put('won')
            time.sleep(0.5)          # hold it while the others try
    except PackLockHeld:
        results.put('refused')


def test_racers_released_together_let_exactly_one_through(tmp_path: Path):
    context = multiprocessing.get_context('spawn')
    barrier, results = context.Barrier(_RACERS), context.Queue()
    racers = [context.Process(target=_race, args=(str(tmp_path), barrier, results)) for _ in range(_RACERS)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(timeout=60)
    outcomes = sorted(results.get(timeout=5) for _ in range(_RACERS))

    assert outcomes == ['refused'] * (_RACERS - 1) + ['won']
    assert list(tmp_path.iterdir()) == []         # the lock is gone, and no temporary was left


def test_the_lock_on_disk_is_always_a_complete_record(tmp_path: Path):
    with PackLock(tmp_path):
        record = json.loads((tmp_path / LOCK_FILENAME).read_text(encoding='utf-8'))
        assert set(record) == {'pid', 'create_time'}
        assert [path.name for path in tmp_path.iterdir()] == [LOCK_FILENAME]
