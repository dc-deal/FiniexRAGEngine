"""Signal packages — the ledger: appended, never rewritten, and the one rule read from it.

`downloaded_whole` is the collector's rule unchanged: a download counts for the span it handed over,
from its range's start, abandoned attempts included; the header's range end is ignored.
"""
import json
from pathlib import Path
from typing import Optional

from finiexragengine.core.packages.package_ledger import PackageLedger, downloaded_whole


def _served(consumer: str, status: int, sent: int, byte_range: Optional[str] = None) -> dict:
    return {'event': 'served', 'consumer': consumer, 'status': status, 'bytes_sent': sent, 'range': byte_range}


def test_events_are_appended_with_a_time_and_read_back_in_order(tmp_path: Path):
    ledger = PackageLedger(tmp_path)
    ledger.append('packed', package_id='stream_a-0001-20260726')
    ledger.append('served', package_id='stream_a-0001-20260726', consumer='ide')
    read = ledger.read()

    assert [e['event'] for e in read.events] == ['packed', 'served']
    assert read.events[0]['at'].endswith('+00:00') and read.unreadable_lines == 0
    assert ledger.read(event='served').count == 1 and ledger.read(package_id='other').count == 0


def test_a_torn_last_line_is_counted_and_never_swallows_the_next_event(tmp_path: Path):
    ledger = PackageLedger(tmp_path)
    ledger.append('packed', package_id='a')
    with open(ledger.path, 'ab') as handle:
        handle.write(b'{"event": "served", "pack')              # a writer killed mid-line
    ledger.append('received', package_id='a')
    read = ledger.read()

    assert [e['event'] for e in read.events] == ['packed', 'received']
    assert read.unreadable_lines == 1
    assert ledger.path.read_bytes().count(b'\n') == 3


def test_a_ledger_that_cannot_be_written_does_not_raise(tmp_path: Path):
    blocker = tmp_path / 'not_a_folder'
    blocker.write_text('a file where the folder should be')
    PackageLedger(blocker).append('packed', package_id='a')     # logged, not raised


def test_seed_books_what_a_dead_run_committed_but_never_booked(tmp_path: Path):
    ledger = PackageLedger(tmp_path)
    manifest = {'package_id': 'stream_a-0001-20260726', 'source': 'stream_a', 'cutoff': 'c',
                'created_at': 't', 'zip': {'name': 'z', 'size_bytes': 1, 'sha256': 's'},
                'member_count': 0, 'record_count': 0, 'members': []}
    assert ledger.seed([manifest]) == ['stream_a-0001-20260726']
    assert ledger.seed([manifest]) == []
    assert ledger.read(event='packed').events[0]['entered_late'] is True


def test_a_whole_download_by_the_same_consumer_counts():
    assert downloaded_whole([_served('ide', 200, 100)], 'ide', 100)
    assert not downloaded_whole([_served('ide', 200, 99)], 'ide', 100)
    assert not downloaded_whole([_served('other', 200, 100)], 'ide', 100)
    assert not downloaded_whole([], 'ide', 0)


def test_resumed_ranges_add_up_and_a_gap_does_not():
    abandoned = _served('ide', 200, 40)                             # cut off after 40 bytes
    resumed = _served('ide', 206, 60, 'bytes=40-')
    assert downloaded_whole([abandoned, resumed], 'ide', 100)
    assert not downloaded_whole([abandoned, _served('ide', 206, 50, 'bytes=50-')], 'ide', 100)
    assert downloaded_whole([_served('ide', 206, 30, 'bytes=-30'), _served('ide', 206, 70, 'bytes=0-69')], 'ide', 100)


def test_a_multi_range_or_malformed_header_counts_for_nothing():
    assert not downloaded_whole([_served('ide', 206, 100, 'bytes=0-49,50-99')], 'ide', 100)
    assert not downloaded_whole([_served('ide', 206, 100, 'items=0-99')], 'ide', 100)


def test_every_line_is_one_json_object(tmp_path: Path):
    ledger = PackageLedger(tmp_path)
    ledger.append('received', package_id='a', note='ünïcode · kept')
    record = json.loads(ledger.path.read_text(encoding='utf-8').splitlines()[0])
    assert record['note'] == 'ünïcode · kept'
