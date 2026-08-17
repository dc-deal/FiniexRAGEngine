"""Rendering tests for the quarantine history report (ISSUE_84).

No database: the report's row shapes are built directly, so the *arithmetic and the wording* — the
parts an operator reads during an incident — are covered everywhere, including where PostgreSQL is
not reachable. The SQL side is exercised by `test_source_health_store.py`, which writes real
episodes through the store.

The numbers below are the measured ones from the two incidents in the issue, so a regression shows
up as a figure that stops matching the story it was derived from.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.source_quarantine_report import (
    QuarantineEpisodeRow,
    SourceQuarantineReport,
    format_quarantine_episode,
    format_source_quarantine_report,
)

# ecb_press polls roughly every 45s — the cadence every cost below is priced in.
_CADENCE = 45.0


def _quarantine(started: datetime, hours: float, rung: int, *, outcome='probe_ok',
                trigger_ms=10_000.0, ended=None) -> QuarantineEpisodeRow:
    return QuarantineEpisodeRow(
        kind='quarantine', source_set='forex_news', started_at=started,
        ended_at=ended if ended is not None else started + timedelta(hours=hours),
        rung=rung, rungs_total=3, cooloff_hours=hours, trigger_type='UNREACHABLE',
        trigger_status=None, trigger_ms=trigger_ms, streak=5, failed_of=None, outcome=outcome,
        cadence_seconds=_CADENCE)


def _correlated(started: datetime, hours: float) -> QuarantineEpisodeRow:
    return QuarantineEpisodeRow(
        kind='correlated', source_set='forex_news', started_at=started,
        ended_at=started + timedelta(hours=hours), rung=None, rungs_total=None,
        cooloff_hours=None, trigger_type='HOST_UNREACHABLE', trigger_status=None, trigger_ms=None,
        streak=None, failed_of='12/12', outcome='resumed', cadence_seconds=_CADENCE)


def _report(rows) -> SourceQuarantineReport:
    report = SourceQuarantineReport(source_id='ecb_press', since_label='30d', rows=rows,
                                    cadence_seconds=_CADENCE)
    quarantines = report.quarantines
    if quarantines:
        report.current_rung = quarantines[-1].rung
        report.current_rungs_total = quarantines[-1].rungs_total
        report.ladder_resets_at = quarantines[-1].started_at + timedelta(hours=168)
    return report


def test_an_episode_is_priced_in_the_feeds_own_polls():
    # The 2026-08-15 incident, replayed under the new policy: one hour instead of a day.
    episode = _quarantine(datetime(2026, 8, 15, 5, 4, 4, tzinfo=timezone.utc), 1.0, rung=0)
    assert episode.polls_missed == 80                     # 3600s / 45s
    assert episode.polls_missed_under_old_policy == 1920   # what the flat 24h would have cost


def test_a_correlated_event_is_never_priced_against_the_policy():
    # The distinction the whole batch is judged on: an outage we did not cause must not appear
    # as a cost we inflicted.
    event = _correlated(datetime(2026, 7, 29, 15, 4, 51, tzinfo=timezone.utc), 5.0)
    assert event.polls_missed_under_old_policy is None
    report = _report([event])
    assert (report.missed_to_policy, report.missed_to_outage) == (0, 400)


def test_the_summary_separates_self_inflicted_loss_from_the_outage():
    rows = [_correlated(datetime(2026, 7, 29, 15, 4, 51, tzinfo=timezone.utc), 5.0),
            _quarantine(datetime(2026, 8, 15, 5, 4, 4, tzinfo=timezone.utc), 1.0, rung=0),
            _quarantine(datetime(2026, 8, 16, 22, 11, 30, tzinfo=timezone.utc), 6.0, rung=1)]
    text = format_source_quarantine_report(_report(rows))

    assert '3 events (2 quarantines, 1 host)' in text
    assert 'polls missed:  560 to policy · 400 to the outage' in text
    assert 'rung now 2/3' in text
    # The correlated row must say what did NOT happen, or it reads as an unexplained gap.
    assert 'no quarantine, no rung advance' in text
    assert '⚠ host 12/12' in text


def test_the_rung_is_rendered_one_based():
    # Stored 0-based (an index into the ladder), read 1-based — "rung 1/3" is the operator's
    # vocabulary, and an off-by-one here would silently understate every escalation.
    text = format_source_quarantine_report(
        _report([_quarantine(datetime(2026, 8, 15, 5, 4, 4, tzinfo=timezone.utc), 1.0, rung=0)]))
    assert ' 1/3 ' in text
    assert ' 0/3 ' not in text


def test_a_feed_that_was_never_held_back_says_so():
    text = format_source_quarantine_report(_report([]))
    assert 'no quarantine episodes in the window' in text


def test_a_missing_table_is_an_empty_answer_not_a_crash():
    report = SourceQuarantineReport(source_id='ecb_press', since_label='30d',
                                    history_missing=True)
    assert 'apply migration 007' in format_source_quarantine_report(report)


def test_a_running_episode_counts_up_to_now():
    # An open episode has no ended_at; the cost must keep accruing rather than reading as zero.
    started = datetime.now(timezone.utc) - timedelta(minutes=30)
    episode = QuarantineEpisodeRow(
        kind='quarantine', source_set='forex_news', started_at=started, ended_at=None, rung=0,
        rungs_total=3, cooloff_hours=1.0, trigger_type='UNREACHABLE', trigger_status=None,
        trigger_ms=10_000.0, streak=5, failed_of=None, outcome=None, cadence_seconds=_CADENCE)
    assert 39 <= episode.polls_missed <= 41
    assert 'running' in format_source_quarantine_report(_report([episode]))


def test_the_episode_view_shows_the_run_up_and_the_comparison():
    started = datetime(2026, 8, 15, 5, 4, 4, tzinfo=timezone.utc)
    episode = _quarantine(started, 1.0, rung=0)
    episode.timeline = [
        {'ts': '2026-08-15T05:00:22+00:00', 'outcome': 'ok', 'duration_ms': 398.0},
        {'ts': '2026-08-15T05:01:03+00:00', 'outcome': 'failed', 'duration_ms': 10002.0,
         'type': 'UNREACHABLE', 'status': None},
    ]
    text = format_quarantine_episode(episode, 'ecb_press')

    assert 'rung 1/3 (1h)' in text
    assert '05:00:22' in text and '05:01:03' in text
    assert 'UNREACHABLE' in text
    assert '5 consecutive failures' in text
    assert 'under the old flat policy: 1920' in text


def test_the_episode_view_falls_back_to_the_frozen_timeline():
    # Past the journal's 14-day retention the frozen `recent_events` copy is all that is left —
    # which is exactly why it is copied into the episode at decision time.
    episode = _quarantine(datetime(2026, 7, 1, 5, 4, 4, tzinfo=timezone.utc), 1.0, rung=0)
    episode.timeline = [{'ts': '2026-07-01T05:04:04+00:00', 'level': 'warning',
                         'type': 'UNREACHABLE', 'status': None, 'message': 'read timed out'}]
    text = format_quarantine_episode(episode, 'ecb_press')
    assert 'read timed out' in text
    assert 'no poll detail' not in text


def test_an_episode_with_nothing_left_says_so_instead_of_rendering_blank():
    episode = _quarantine(datetime(2026, 7, 1, 5, 4, 4, tzinfo=timezone.utc), 1.0, rung=0)
    assert 'no poll detail' in format_quarantine_episode(episode, 'ecb_press')
