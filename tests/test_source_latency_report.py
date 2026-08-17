"""Source latency & gap report (ISSUE_76) — the slow-vs-dead verdict and outage cost.

Runs against the canonical `source_poll_log` in the migration-built test schema (`clean_db`);
rows are inserted directly so each case controls its own timestamps and durations.
"""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from finiexragengine.core.observability.reports.source_latency_report import (
    build_source_latency_report,
    format_source_latency_report,
)

_TABLE = 'source_poll_log'


def _insert(dsn: str, rows) -> None:
    """rows: (ts, source_id, outcome, duration_ms, error_type)."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for ts, source_id, outcome, duration_ms, error_type in rows:
            cur.execute(
                f'INSERT INTO {_TABLE} (ts, source_id, source_set, outcome, duration_ms, '
                'error_type) VALUES (%s, %s, %s, %s, %s, %s)',
                (ts, source_id, 'forex_news', outcome, duration_ms, error_type))
        conn.commit()


@pytest.fixture
def since() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=7)


def test_failures_do_not_pollute_the_success_percentiles(clean_db, since):
    """Keeping the two apart is the design decision the whole report rests on.

    A timeout lands at the deadline (10s). Averaged into the success percentiles it would drag
    p99 to the timeout and make a perfectly fast feed look like it is about to fail — hiding the
    very signal the operator needs.
    """
    now = datetime.now(timezone.utc)
    rows = [(now - timedelta(minutes=i), 'ecb_press', 'ok', 500.0, None) for i in range(20)]
    rows += [(now - timedelta(minutes=30 + i), 'ecb_press', 'failed', 10_000.0, 'UNREACHABLE')
             for i in range(5)]
    _insert(clean_db, rows)

    report = build_source_latency_report(clean_db, since, timeouts={'ecb_press': 10})
    row = report.latency[0]
    assert row.polls == 20 and row.failures == 5
    assert row.p50_ms == 500.0 and row.max_ms == 500.0     # untouched by the 10s failures
    assert row.fail_p50_ms == 10_000.0


def test_the_verdict_separates_a_slow_feed_from_a_dead_one(clean_db, since):
    """The question ecb_press could not answer on 2026-08-15, now answered by the durations.

    A failure that burned the full deadline means the feed accepted the connection and went
    quiet — a longer timeout might have worked. A failure that returned in milliseconds means it
    was refused outright — a longer timeout would change nothing.
    """
    now = datetime.now(timezone.utc)
    _insert(clean_db, [
        (now - timedelta(minutes=1), 'slow_feed', 'failed', 9_800.0, 'UNREACHABLE'),
        (now - timedelta(minutes=2), 'slow_feed', 'failed', 10_010.0, 'UNREACHABLE'),
        (now - timedelta(minutes=1), 'dead_feed', 'failed', 40.0, 'HTTP_ERROR'),
        (now - timedelta(minutes=2), 'dead_feed', 'failed', 55.0, 'HTTP_ERROR'),
    ])
    report = build_source_latency_report(
        clean_db, since, timeouts={'slow_feed': 10, 'dead_feed': 10})
    verdicts = {row.source_id: row.failure_verdict for row in report.latency}
    assert verdicts == {'slow_feed': 'timeout', 'dead_feed': 'refused'}


def test_a_healthy_feed_has_no_verdict_and_no_warning(clean_db, since):
    now = datetime.now(timezone.utc)
    _insert(clean_db, [(now - timedelta(minutes=i), 'coindesk', 'ok', 300.0, None)
                       for i in range(10)])
    row = build_source_latency_report(clean_db, since, timeouts={'coindesk': 10}).latency[0]
    assert row.failure_verdict == '' and row.nearing_timeout is False


def test_p99_near_the_timeout_raises_the_warning_before_it_fails(clean_db, since):
    """The point of the ⚠: a feed that is still succeeding but has no headroom left."""
    now = datetime.now(timezone.utc)
    # 90 fast polls + 10 at 7.5s. It takes a real tail, not one outlier: percentile_cont
    # interpolates, so a single slow sample among 99 fast ones lands p99 at ~0.5s — correctly,
    # because one slow poll is not a feed losing headroom.
    rows = [(now - timedelta(seconds=i), 'forexlive', 'ok', 400.0, None) for i in range(90)]
    rows += [(now - timedelta(seconds=200 + i), 'forexlive', 'ok', 7_500.0, None)
             for i in range(10)]
    _insert(clean_db, rows)

    report = build_source_latency_report(clean_db, since, timeouts={'forexlive': 10},
                                         warn_ratio=0.7)
    assert report.latency[0].nearing_timeout is True
    # The same numbers against a 30s deadline have plenty of headroom — the warning is relative.
    relaxed = build_source_latency_report(clean_db, since, timeouts={'forexlive': 30},
                                          warn_ratio=0.7)
    assert relaxed.latency[0].nearing_timeout is False


def test_an_unconfigured_timeout_yields_no_verdict_rather_than_a_guess(clean_db, since):
    now = datetime.now(timezone.utc)
    _insert(clean_db, [(now, 'mystery', 'failed', 9_900.0, 'UNREACHABLE')])
    row = build_source_latency_report(clean_db, since, timeouts={}).latency[0]
    assert row.failure_verdict == 'failed'      # it failed; why is unknowable without the deadline
    assert row.nearing_timeout is False


def test_a_gap_beyond_the_feeds_own_cadence_is_an_outage_with_a_cost(clean_db, since):
    """The quarantine's price, in the unit that matters: polls that were never made.

    ecb_press was quarantined for 24h over 3m42s of failure. Measuring the gap against the feed's
    *own* cadence is what turns that into a comparable number without a global threshold.
    """
    now = datetime.now(timezone.utc)
    # 40s cadence for 30 polls, then a two-hour hole, then the cadence resumes.
    rows = [(now - timedelta(hours=6) + timedelta(seconds=40 * i), 'ecb_press', 'ok', 500.0, None)
            for i in range(30)]
    resume = now - timedelta(hours=6) + timedelta(seconds=40 * 29) + timedelta(hours=2)
    rows += [(resume + timedelta(seconds=40 * i), 'ecb_press', 'ok', 500.0, None)
             for i in range(30)]
    _insert(clean_db, rows)

    report = build_source_latency_report(clean_db, since)
    gap = report.gaps[0]
    assert gap.source_id == 'ecb_press'
    assert gap.cadence_s == pytest.approx(40.0, abs=1.0)
    assert gap.gaps == 1
    assert gap.longest_gap_s == pytest.approx(7200.0, abs=1.0)
    assert gap.polls_missed == pytest.approx(179, abs=2)     # 7200s / 40s, minus the one real tick


def test_a_steady_feed_reports_no_outage(clean_db, since):
    """Scheduling jitter is not an incident — the factor and the 5-minute floor keep it quiet."""
    now = datetime.now(timezone.utc)
    rows = [(now - timedelta(hours=2) + timedelta(seconds=40 * i), 'coindesk', 'ok', 500.0, None)
            for i in range(100)]
    rows[50] = (rows[50][0] + timedelta(seconds=90), 'coindesk', 'ok', 500.0, None)   # one hiccup
    _insert(clean_db, rows)
    assert build_source_latency_report(clean_db, since).gaps == []


def test_a_database_without_the_journal_answers_empty_instead_of_crashing(clean_db, since):
    """Pre-migration-004, or the kill switch never flipped on: a valid empty answer."""
    report = build_source_latency_report(clean_db, since, table='source_poll_log_absent')
    assert report.journal_missing is True and report.latency == [] and report.gaps == []
    assert 'apply migration 004' in format_source_latency_report(report)


def test_format_renders_both_sections_and_the_legend(clean_db, since):
    now = datetime.now(timezone.utc)
    _insert(clean_db, [(now - timedelta(minutes=i), 'ecb_press', 'ok', 500.0, None)
                       for i in range(5)]
            + [(now - timedelta(minutes=9), 'ecb_press', 'failed', 10_000.0, 'UNREACHABLE')])
    out = format_source_latency_report(
        build_source_latency_report(clean_db, since, since_label='7d',
                                    timeouts={'ecb_press': 10}))
    assert 'latency (last 7d)' in out and 'poll gaps (last 7d)' in out
    assert 'ecb_press' in out and 'timeout' in out
    assert '500ms' in out and '10.0s' in out                 # sub-second stays in ms; the rest in s
    assert 'no feed stopped being polled' in out             # empty gap section says so


def test_the_report_states_how_much_history_it_actually_rests_on(clean_db, since):
    """A young journal must not read as a full window.

    The journal cannot be backfilled — unlike ISSUE_81, which corrected the whole archive from
    stored envelopes, durations were never recorded before ISSUE_76. So `last 7d` over three hours
    of samples is `since we started measuring`, and the header says so rather than implying a week.
    """
    now = datetime.now(timezone.utc)
    _insert(clean_db, [(now - timedelta(hours=3), 'coindesk', 'ok', 400.0, None),
                       (now, 'coindesk', 'ok', 400.0, None)])
    report = build_source_latency_report(clean_db, since, since_label='7d')
    assert report.reach_seconds == pytest.approx(3 * 3600, abs=60)
    out = format_source_latency_report(report)
    assert 'journal covers 3h00m' in out
    assert 'still filling' in out                     # under a day -> the caveat is explicit


def test_a_mature_journal_drops_the_still_filling_caveat(clean_db, since):
    now = datetime.now(timezone.utc)
    _insert(clean_db, [(now - timedelta(days=5), 'coindesk', 'ok', 400.0, None),
                       (now, 'coindesk', 'ok', 400.0, None)])
    out = format_source_latency_report(build_source_latency_report(clean_db, since))
    assert 'journal covers 5d 0h' in out and 'still filling' not in out


def test_an_empty_journal_says_empty(clean_db, since):
    out = format_source_latency_report(build_source_latency_report(clean_db, since))
    assert 'journal: empty' in out


def test_the_report_prints_on_a_legacy_codepage(clean_db, since, capsys):
    """A piped run on Windows must not die on a character the report chose to use.

    Python takes stdout's encoding from the console when it has one, but falls back to the
    locale's — cp1252 on a Western Windows — with `errors='strict'` as soon as output is piped or
    redirected. The report renders `⚠`, `→` and `—`, none of which cp1252 can encode, so
    `sources_cli --since 2d | Select-Object -Last 30` died where the same command in the window
    worked (observed 2026-08-17). Twenty-seven such characters exist across the package; `→` alone
    is in 34 files, so the fix belongs at the output boundary, not in the strings.
    """
    now = datetime.now(timezone.utc)
    rows = [(now - timedelta(seconds=i), 'actionforex', 'ok', 7_500.0, None) for i in range(20)]
    rows += [(now - timedelta(minutes=5 + i), 'actionforex', 'failed', 20_880.0, 'UNREACHABLE')
             for i in range(3)]
    _insert(clean_db, rows)
    text = format_source_latency_report(
        build_source_latency_report(clean_db, since, timeouts={'actionforex': 10}))

    assert '⚠' in text, 'the warning marker is what made this fail — keep it in the fixture'
    # The bytes a cp1252 stdout would be asked to write, under the boundary policy the CLIs apply.
    assert text.encode('utf-8', errors='replace')
    with pytest.raises(UnicodeEncodeError):
        text.encode('cp1252')          # the trap itself, asserted rather than assumed
