"""Config generations (ISSUE_116) — the activation timeline, tested where it can be wrong.

The report's whole reason is that two edge points are not a span, so the aggregation is what is
asserted here: a span closes at the next activation **on its own stream**, the current one stays
open, and what a generation produced is counted from the envelopes' own fingerprint stamp rather
than assumed from the span. `assign_spans` is DB-free, so none of this needs Postgres.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.generations_report import (
    Activation,
    GenerationsReport,
    assign_spans,
    format_generations_report,
)

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_SINCE = _NOW - timedelta(days=30)


def _activation(minutes: int, fingerprint: str, reason: str = 'boot',
                pipeline_id: str = 'crypto_sentiment') -> Activation:
    return Activation(pipeline_id=pipeline_id, fingerprint=fingerprint, reason=reason,
                      activated_at=_NOW + timedelta(minutes=minutes), process_started_at=_NOW)


def _stamp(minutes: int, fingerprint: str, pipeline_id: str = 'crypto_sentiment'):
    return (pipeline_id, _NOW + timedelta(minutes=minutes), fingerprint)


def _report(rows) -> GenerationsReport:
    report = GenerationsReport(since_label='30d')
    report.rows = rows
    return report


def test_a_span_closes_at_the_next_activation_and_the_last_one_stays_open():
    rows = assign_spans([_activation(0, 'aaa'), _activation(30, 'bbb')], [], _SINCE)

    newest, oldest = rows                                  # newest first
    assert (newest.fingerprint, newest.current) == ('bbb', True)
    assert oldest.ran_until == newest.activated_at and not oldest.current


def test_streams_do_not_close_each_others_spans():
    """Streams activate independently — an ingest deploy restarts both, and the rows are neighbours
    only by accident of ordering. Closing one stream's span at the other's activation would invent
    a generation change nobody made."""
    rows = assign_spans([_activation(0, 'aaa', pipeline_id='crypto_sentiment'),
                         _activation(5, 'bbb', pipeline_id='forex_macro_sentiment')], [], _SINCE)

    assert all(row.current for row in rows)                # each stream's only activation is open


def test_what_a_generation_produced_is_counted_from_the_envelope_stamp():
    """Both conditions, never one: inside the span AND carrying that fingerprint."""
    rows = assign_spans([_activation(0, 'aaa'), _activation(30, 'bbb')],
                        [_stamp(5, 'aaa'), _stamp(10, 'aaa'), _stamp(40, 'bbb'),
                         _stamp(50, 'aaa')],           # a stray stamp: right span, wrong generation
                        _SINCE)

    by_fingerprint = {row.fingerprint: row.envelopes for row in rows}
    assert by_fingerprint == {'aaa': 2, 'bbb': 1}


def test_a_generation_that_produced_nothing_is_marked():
    """The reverted single-pass excursion of 2026-08-27, which no ordering check can find."""
    rows = assign_spans([_activation(0, 'aaa'), _activation(8, 'bbb'), _activation(29, 'aaa')],
                        [_stamp(3, 'aaa'), _stamp(40, 'aaa')], _SINCE)
    text = format_generations_report(_report(rows), width=110)

    excursion = next(row for row in rows if row.fingerprint == 'bbb')
    assert excursion.silent and excursion.envelopes == 0
    assert '⚠️ produced nothing' in text
    # A currently-live generation with no envelopes yet is NOT the same finding and is not marked.
    assert sum(1 for row in rows if row.silent and not row.current) == 1


def test_an_activation_older_than_the_window_is_carried_in_not_dropped():
    """It is what explains the envelopes at the window's start; dropping it leaves them orphaned."""
    rows = assign_spans([Activation('crypto_sentiment', 'aaa', 'boot', _SINCE - timedelta(days=2)),
                         _activation(0, 'bbb')],
                        [(('crypto_sentiment'), _SINCE + timedelta(minutes=1), 'aaa')], _SINCE)

    carried = next(row for row in rows if row.fingerprint == 'aaa')
    assert carried.carried_in and carried.envelopes == 1
    assert '(carried into the window)' in format_generations_report(_report(rows), width=110)


def test_the_footer_names_the_current_generation_per_stream_and_counts_rollbacks():
    rows = assign_spans([_activation(0, 'aaa'), _activation(8, 'bbb'),
                         _activation(29, 'aaa', reason='rollback')], [], _SINCE)
    text = format_generations_report(_report(rows), width=110)

    assert '1 rollback(s)' in text and '3 activation(s)' in text
    assert 'crypto_sentiment: aaa live since' in text


def test_a_database_without_the_migration_says_so_rather_than_reporting_an_empty_timeline():
    """"No activation recorded" and "this engine records none" are different answers."""
    missing = GenerationsReport(since_label='30d', table_missing=True)
    empty = GenerationsReport(since_label='30d')

    assert 'migration 015 has not run here' in format_generations_report(missing, width=110)
    assert 'no activation in this window' in format_generations_report(empty, width=110)
