"""Reading the engine log over a UTC range (2026-09-08) — the unit, with no HTTP and no real log.

The route exists because four connectivity outages that day had their cause in one word inside a
traceback, and reading it needed an RDP session. The cases below are the ways that read can be
quietly wrong rather than loudly broken.
"""
from datetime import datetime, timezone
from pathlib import Path

from finiexragengine.core.observability.log_reader import (
    files_for_range,
    parse_timestamp,
    read_log,
)


def _utc(hour: int, minute: int = 0, second: int = 0, day: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, minute, second, tzinfo=timezone.utc)


def _write(path: Path, *lines: str) -> Path:
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


# --- the timezone, which is the whole difficulty --------------------------------------------

def test_a_local_stamped_line_is_matched_by_its_UTC_instant_not_its_digits(tmp_path):
    """The case that would have shipped wrong.

    The formatter writes the OS clock (server = GMT+2) while the engine and every other API surface
    speak UTC. A line reading `11:05:03+02:00` happened at **09:05:03 UTC** — so it belongs to a
    09:00–09:10 UTC window and NOT to an 11:00–11:10 one, however much the digits suggest
    otherwise. A naive string compare gets this backwards and says nothing.
    """
    log = _write(tmp_path / 'finiex.log',
                 '2026-09-08T11:05:03.750+02:00 ERROR mod: [HOST] host connectivity — 11/11')

    inside = read_log(log, since=_utc(9, 0), until=_utc(9, 10), min_level='INFO')
    outside = read_log(log, since=_utc(11, 0), until=_utc(11, 10), min_level='INFO')

    assert [e.message for e in inside.entries] == ['mod: [HOST] host connectivity — 11/11']
    assert outside.entries == []


def test_the_returned_timestamp_is_UTC_so_it_can_be_compared_with_every_other_surface(tmp_path):
    log = _write(tmp_path / 'finiex.log',
                 '2026-09-08T11:05:03.750+02:00 ERROR mod: something')
    entry = read_log(log, min_level='INFO').entries[0]

    assert entry.timestamp == datetime(2026, 9, 8, 9, 5, 3, 750000, tzinfo=timezone.utc)
    assert entry.timestamp.utcoffset().total_seconds() == 0


def test_a_stamp_without_an_offset_is_read_as_UTC_rather_than_guessed(tmp_path):
    """Guessing the writer's zone is how a two-hour error becomes invisible."""
    assert parse_timestamp('2026-09-08T09:05:03') == _utc(9, 5, 3)
    assert parse_timestamp('2026-09-08T09:05:03Z') == _utc(9, 5, 3)
    assert parse_timestamp('not a timestamp') is None


# --- rotation: a range is not one file -------------------------------------------------------

def test_a_range_reaching_past_midnight_reads_the_rotated_sibling_too(tmp_path):
    """Otherwise "query a time range" quietly means "query today"."""
    _write(tmp_path / 'finiex.log.2026-09-07',
           '2026-09-07T23:50:00.000+02:00 ERROR mod: yesterday late')
    log = _write(tmp_path / 'finiex.log',
                 '2026-09-08T01:10:00.000+02:00 ERROR mod: today early')

    page = read_log(log, since=_utc(20, 0, day=7), until=_utc(2, 0), min_level='INFO')

    assert [e.message for e in page.entries] == ['mod: today early', 'mod: yesterday late']
    assert len(page.files_read) == 2


def test_entries_arrive_chronologically_across_files_and_are_returned_newest_first(tmp_path):
    _write(tmp_path / 'finiex.log.2026-09-07', '2026-09-07T10:00:00.000+02:00 ERROR mod: older')
    log = _write(tmp_path / 'finiex.log', '2026-09-08T10:00:00.000+02:00 ERROR mod: newer')

    entries = read_log(log, min_level='INFO').entries

    assert [e.message for e in entries] == ['mod: newer', 'mod: older']


def test_a_sibling_that_is_not_a_daily_rotation_is_ignored(tmp_path):
    log = tmp_path / 'finiex.log'
    log.write_text('', encoding='utf-8')
    (tmp_path / 'finiex.log.1').write_text('x', encoding='utf-8')
    (tmp_path / 'finiex.log.gz').write_text('x', encoding='utf-8')

    assert [p.name for p in files_for_range(log, None, None)] == ['finiex.log']


# --- a traceback belongs to its entry --------------------------------------------------------

def test_continuation_lines_travel_with_their_entry(tmp_path):
    """A stack fragment with no head is what made the filtered log useless on 2026-09-08."""
    log = _write(
        tmp_path / 'finiex.log',
        '2026-09-08T11:05:03.768+02:00 ERROR mod: [HOST] alert delivery failed',
        'Traceback (most recent call last):',
        '  File "telegram_client.py", line 79, in _call',
        'TelegramError: sendMessage failed: could not reach api.telegram.org',
        '2026-09-08T11:05:06.000+02:00 INFO mod: unrelated')

    entry = read_log(log, min_level='ERROR').entries[0]

    assert entry.message.endswith('alert delivery failed')
    assert len(entry.continuation) == 3
    assert 'TelegramError' in entry.continuation[-1]
    assert entry.lines == 4


def test_a_traceback_under_a_filtered_out_entry_is_dropped_with_it(tmp_path):
    """Not orphaned onto the next kept entry, which would attribute one failure's stack to another."""
    log = _write(
        tmp_path / 'finiex.log',
        '2026-09-08T11:00:00.000+02:00 INFO mod: chatty',
        '  some detail nobody asked for',
        '2026-09-08T11:01:00.000+02:00 ERROR mod: the real problem')

    entries = read_log(log, min_level='ERROR').entries

    assert len(entries) == 1
    assert entries[0].continuation == []


# --- level, limit, and saying what was left out ----------------------------------------------

def test_min_level_is_a_floor_not_an_exact_match(tmp_path):
    log = _write(tmp_path / 'finiex.log',
                 '2026-09-08T10:00:00.000+02:00 DEBUG mod: d',
                 '2026-09-08T10:00:01.000+02:00 INFO mod: i',
                 '2026-09-08T10:00:02.000+02:00 WARNING mod: w',
                 '2026-09-08T10:00:03.000+02:00 ERROR mod: e')

    assert len(read_log(log, min_level='DEBUG').entries) == 4
    assert len(read_log(log, min_level='WARNING').entries) == 2
    assert len(read_log(log, min_level='ERROR').entries) == 1


def test_the_limit_keeps_the_NEWEST_end_and_says_it_truncated(tmp_path):
    """During an incident the question is "what just happened", never "what happened first"."""
    log = _write(tmp_path / 'finiex.log',
                 *[f'2026-09-08T10:00:0{i}.000+02:00 ERROR mod: line {i}' for i in range(5)])

    page = read_log(log, min_level='ERROR', limit=2)

    assert [e.message for e in page.entries] == ['mod: line 4', 'mod: line 3']
    assert page.matched == 5 and page.truncated


def test_a_range_with_nothing_in_it_is_an_empty_page_not_an_error(tmp_path):
    log = _write(tmp_path / 'finiex.log', '2026-09-08T10:00:00.000+02:00 ERROR mod: e')

    page = read_log(log, since=_utc(20, 0), min_level='ERROR')

    assert page.entries == [] and page.matched == 0 and not page.truncated


# --- redaction, and it announces itself -------------------------------------------------------

def test_an_ordinary_line_is_untouched_and_not_counted(tmp_path):
    log = _write(tmp_path / 'finiex.log',
                 '2026-09-08T10:00:00.000+02:00 ERROR mod: cannot fetch feed (getaddrinfo failed)')

    page = read_log(log, min_level='ERROR')

    assert page.redacted_lines == 0
    assert 'getaddrinfo failed' in page.entries[0].message


def test_redaction_is_counted_including_inside_a_traceback(tmp_path):
    """A silently altered line is worse than a withheld one — the reader trusts it."""
    log = _write(tmp_path / 'finiex.log',
                 '2026-09-08T10:00:00.000+02:00 ERROR mod: db down',
                 '  psycopg.OperationalError: postgresql://u:s3cret@db:5432/rag')

    page = read_log(log, min_level='ERROR')

    assert page.redacted_lines == 1
    assert 's3cret' not in page.entries[0].continuation[0]
