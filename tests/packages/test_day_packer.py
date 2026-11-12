"""Signal packages — one run for one source: closed days from the journal into one zip.

Real PostgreSQL through `clean_db` (skipped without one); the zip and the manifest on `tmp_path`.
Pipeline names are neutral (`stream_a`, `stream_b`), as every public fixture's are.
"""
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import psycopg
import pytest

from finiexragengine.core.outcome.outcome_exporter import OutcomeArchiveExporter
from finiexragengine.core.outcome.outcome_store import OutcomeStore
from finiexragengine.core.packages.day_packer import PackContext, default_cutoff, pack_source
from finiexragengine.core.packages.pack_lock import PackLock
from finiexragengine.core.packages.package_ledger import PackageLedger
from finiexragengine.core.packages.package_store import PackageStore
from finiexragengine.types.outcome_types import RunMetadata, SentimentEnvelope, SentimentResult

_SUNDAY = datetime(2026, 7, 26, 1, 30, tzinfo=timezone.utc)
_CUTOFF = datetime(2026, 7, 26, tzinfo=timezone.utc)
_PACKER = {'program': 'finiex-rag-packages', 'version': 'test', 'commit': 'abc1234', 'dirty': False}


def _env(pipeline_id: str, ts: datetime) -> SentimentEnvelope:
    return SentimentEnvelope(
        pipeline_id=pipeline_id, outcome_type='sentiment_fear_greed', prompt_version='2',
        prompt_id='sentiment-test', prompt_hash='1c86eac137d8', timestamp=ts, status='success',
        result=[SentimentResult(symbol='BTCUSD', signal='BUY', sentiment_score=0.4,
                                confidence=0.8, reasoning='r')],
        metadata=RunMetadata(model='gpt-4o-mini', model_snapshot='gpt-4o-mini-2024-07-18'))


def _save(dsn: str, pipeline_id: str, *moments: datetime) -> None:
    store = OutcomeStore(dsn)
    for moment in moments:
        store.save(_env(pipeline_id, moment))


def _at(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc)


@pytest.fixture
def context(clean_db: str, tmp_path: Path) -> PackContext:
    root = tmp_path / 'packages'
    return PackContext(store=PackageStore(root), ledger=PackageLedger(root),
                       exporter=OutcomeArchiveExporter(clean_db), database_url=clean_db,
                       mint=OutcomeStore(clean_db).instance_identity(), packer=_PACKER,
                       min_free_percent=0)


def _members(zip_path: Path) -> List[str]:
    with zipfile.ZipFile(zip_path) as archive:
        return sorted(archive.namelist())


def test_closed_days_before_the_cutoff_become_one_package(clean_db, context):
    _save(clean_db, 'stream_a', _at(20), _at(20, 11), _at(22), _at(26))     # the 26th is the cutoff day
    run = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)

    assert run.result == 'packed', run.reason
    assert run.package_id == 'stream_a-0001-20260726'
    found = context.store.find(run.package_id)
    assert found is not None and found.state == 'ready'
    assert _members(found.zip_path) == ['stream_a_2026-07-20.jsonl', 'stream_a_2026-07-22.jsonl']
    manifest = found.manifest
    assert (manifest['source'], manifest['sequence'], manifest['previous_package_id']) == ('stream_a', 1, None)
    assert manifest['member_count'] == 2 and manifest['record_count'] == 3
    assert manifest['span'] == {'from': '2026-07-20T00:00:00+00:00', 'until': '2026-07-26T00:00:00+00:00'}
    assert manifest['held_for_next'] == [] and manifest['open_files'] == [] and manifest['excluded'] == []
    assert manifest['zip']['sha256'] == hashlib.sha256(found.zip_path.read_bytes()).hexdigest()
    assert manifest['zip']['size_bytes'] == found.zip_path.stat().st_size


def test_a_member_is_the_export_file_of_its_day_byte_for_byte(clean_db, context, tmp_path):
    """The Testing IDE imports the files it held by hand unchanged; a member must be exactly that."""
    _save(clean_db, 'stream_a', _at(20), _at(20, 11), _at(21))
    run = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)
    OutcomeArchiveExporter(clean_db).export(tmp_path / 'export', now=_SUNDAY)

    with zipfile.ZipFile(context.store.find(run.package_id).zip_path) as archive:
        member = archive.read('stream_a_2026-07-20.jsonl')
    assert member == (tmp_path / 'export' / 'stream_a' / '2026-07-20.jsonl').read_bytes()
    entry = next(m for m in context.store.find(run.package_id).manifest['members']
                 if m['name'] == 'stream_a_2026-07-20.jsonl')
    assert entry['sha256'] == hashlib.sha256(member).hexdigest() and entry['size_bytes'] == len(member)
    assert entry['record_count'] == 2


def test_no_day_is_packed_twice_and_the_chain_links(clean_db, context):
    _save(clean_db, 'stream_a', _at(20), _at(21))
    first = pack_source('stream_a', context, datetime(2026, 7, 21, tzinfo=timezone.utc), now=_SUNDAY)
    _save(clean_db, 'stream_a', _at(23))
    second = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)
    third = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)

    assert (first.result, second.result, third.result) == ('packed', 'packed', 'nothing_new')
    assert _members(context.store.find(first.package_id).zip_path) == ['stream_a_2026-07-20.jsonl']
    later = context.store.find(second.package_id).manifest
    assert [m['name'] for m in later['members']] == ['stream_a_2026-07-21.jsonl', 'stream_a_2026-07-23.jsonl']
    assert (later['sequence'], later['previous_package_id']) == (2, first.package_id)
    assert later['span']['from'] == '2026-07-21T00:00:00+00:00'      # the previous package's cutoff


def test_each_source_is_its_own_chain(clean_db, context):
    _save(clean_db, 'stream_a', _at(20))
    _save(clean_db, 'stream_b', _at(20), _at(21))
    a = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)
    b = pack_source('stream_b', context, _CUTOFF, now=_SUNDAY)

    assert (a.package_id, b.package_id) == ('stream_a-0001-20260726', 'stream_b-0001-20260726')
    assert _members(context.store.find(b.package_id).zip_path) == ['stream_b_2026-07-20.jsonl',
                                                                     'stream_b_2026-07-21.jsonl']


def test_a_day_of_another_producer_is_excluded_with_both_ids(clean_db, context):
    _save(clean_db, 'stream_a', _at(20), _at(21))
    with psycopg.connect(clean_db) as conn:
        conn.execute("UPDATE outcomes SET envelope = jsonb_set(envelope, '{instance_id}', '\"foreign0001\"') "
                     "WHERE ts::date = '2026-07-21'")
    run = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)

    manifest = context.store.find(run.package_id).manifest
    assert [m['name'] for m in manifest['members']] == ['stream_a_2026-07-20.jsonl']
    assert manifest['excluded'][0]['name'] == 'stream_a_2026-07-21.jsonl'
    assert 'foreign0001' in manifest['excluded'][0]['reason'] and context.mint in manifest['excluded'][0]['reason']


@pytest.mark.parametrize('cutoff, now, why', [
    (datetime(2026, 7, 26, 6, 0, tzinfo=timezone.utc), _SUNDAY, 'not a UTC midnight'),
    (datetime(2026, 7, 27, tzinfo=timezone.utc), _SUNDAY, 'has not passed yet'),
    (_CUTOFF, datetime(2026, 7, 26, 0, 10, tzinfo=timezone.utc), 'quiet minutes'),
])
def test_a_cutoff_that_cannot_be_packed_is_refused(clean_db, context, cutoff, now, why):
    _save(clean_db, 'stream_a', _at(20))
    run = pack_source('stream_a', context, cutoff, now=now)

    assert run.result == 'refused' and why in run.reason
    assert context.store.manifests('stream_a') == []


def test_no_mint_no_package(clean_db, context):
    _save(clean_db, 'stream_a', _at(20))
    context.mint = None
    run = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)

    assert run.result == 'refused' and 'no mint' in run.reason
    assert not context.store.ready_dir('stream_a').exists() or not list(context.store.ready_dir('stream_a').iterdir())


def test_a_held_lock_refuses_a_second_packer(clean_db, context):
    _save(clean_db, 'stream_a', _at(20))
    with PackLock(context.store.source_dir('stream_a')):
        run = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)
    assert run.result == 'refused' and 'pack lock held' in run.reason


def test_a_dead_holders_lock_is_taken_over(clean_db, context):
    _save(clean_db, 'stream_a', _at(20))
    folder = context.store.source_dir('stream_a')
    folder.mkdir(parents=True)
    (folder / '.pack.lock').write_text(json.dumps({'pid': 999999, 'create_time': 1.0}))
    assert pack_source('stream_a', context, _CUTOFF, now=_SUNDAY).result == 'packed'
    assert not (folder / '.pack.lock').exists()


def test_what_a_dead_run_left_is_cleaned_and_a_late_ledger_is_repaired(clean_db, context):
    _save(clean_db, 'stream_a', _at(20))
    first = pack_source('stream_a', context, datetime(2026, 7, 21, tzinfo=timezone.utc), now=_SUNDAY)
    context.ledger.path.unlink()                                    # the booking was lost
    ready = context.store.ready_dir('stream_a')
    (ready / 'stream_a-0002-20260726.zip.part').write_bytes(b'half')
    (ready / 'stream_a-0009-20260726.zip').write_bytes(b'no manifest')
    _save(clean_db, 'stream_a', _at(22))
    run = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)

    assert sorted(run.removed_incomplete) == ['stream_a-0002-20260726.zip.part', 'stream_a-0009-20260726.zip']
    assert run.entered_late == [first.package_id]
    packed = [event['package_id'] for event in context.ledger.read(event='packed').events]
    assert packed == [first.package_id, run.package_id]


def test_the_ledger_books_packed_after_the_commit(clean_db, context):
    _save(clean_db, 'stream_a', _at(20))
    run = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)
    event = context.ledger.read(event='packed').events[0]

    assert event['package_id'] == run.package_id
    assert event['members'][0]['name'] == 'stream_a_2026-07-20.jsonl'
    assert event['zip']['sha256'] == context.store.find(run.package_id).manifest['zip']['sha256']


def test_the_last_run_is_recorded_per_source(clean_db, context):
    _save(clean_db, 'stream_a', _at(20))
    pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)
    pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)
    assert context.store.last_runs()['stream_a']['result'] == 'nothing_new'


def test_span_records_are_objects_with_days_missing_when_their_record_is_short(clean_db, context):
    """No sample and no activation in the test journal: both say so per day, neither claims 'none'."""
    _save(clean_db, 'stream_a', _at(20))
    run = pack_source('stream_a', context, _CUTOFF, now=_SUNDAY)
    manifest = context.store.find(run.package_id).manifest

    span_days = [f'2026-07-{day}' for day in range(20, 26)]
    assert manifest['outages'] == {'count': 0, 'days_missing': span_days, 'records': []}
    assert manifest['process_starts'] == {'count': 0, 'days_missing': span_days, 'starts': []}


def test_the_default_cutoff_is_the_utc_midnight_at_or_before_now():
    assert default_cutoff(_SUNDAY) == _CUTOFF
    assert default_cutoff(_CUTOFF) == _CUTOFF
    assert default_cutoff(_CUTOFF - timedelta(microseconds=1)) == _CUTOFF - timedelta(days=1)
