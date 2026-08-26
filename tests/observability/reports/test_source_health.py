"""Source-health: pure logic — host normalization, report formatting, orphan notice, the
feed-doctor classifier. No DB, no network, no API budget (the DB path is test_source_health_store).
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.source_health_report import (
    SourceHealthReport,
    SourceHealthRow,
    format_source_health_report,
)
from finiexragengine.core.observability.source_health_store import _level_for
from finiexragengine.core.sources.feed_doctor import (
    DEFAULT_MAX_AGE_HOURS,
    FeedDiagnosis,
    _scan_suspicious,
    classify_feed,
    format_diagnoses,
)
from finiexragengine.utils.url import normalize_host

_NOW = datetime.now(timezone.utc)


def _row(source_id, **kw):
    base = dict(host=f'{source_id}.com', source_set='crypto_news', total_polls=100,
                total_success=100, total_failures=0, consecutive_failures=0,
                last_success_at=_NOW, last_failure_at=None, last_status=200,
                last_error_type=None, flagged=False, quarantined_until=None, recent_events=[])
    base.update(kw)
    return SourceHealthRow(source_id=source_id, **base)


# --- host normalization ---------------------------------------------------------------

def test_normalize_host_strips_www_port_scheme():
    assert normalize_host('https://www.CryptoSlate.com:443/feed/') == 'cryptoslate.com'
    assert normalize_host('http://feeds.example.org/rss') == 'feeds.example.org'
    assert normalize_host('not a url') == ''


def test_level_split_transient_vs_broken():
    assert _level_for('RATE_LIMITED') == 'warning'      # external throttling, we back off
    assert _level_for('UNREACHABLE') == 'warning'       # transient TLS/transport
    assert _level_for('PARSE_ERROR') == 'error'         # the feed body itself is wrong
    assert _level_for('HTTP_ERROR') == 'error'


# --- row derived state ----------------------------------------------------------------

def test_success_rate_and_quarantined_flag():
    row = _row('x', total_polls=10, total_success=4)
    assert abs(row.success_rate - 0.4) < 1e-9
    assert _row('y', total_polls=0, total_success=0).success_rate is None
    future = _row('z', quarantined_until=_NOW + timedelta(hours=5))
    past = _row('z', quarantined_until=_NOW - timedelta(hours=5))
    assert future.quarantined and not past.quarantined


# --- report formatting ----------------------------------------------------------------

def test_format_shows_flag_quarantine_and_counts():
    flagged = _row('cryptoslate', total_polls=50, total_success=20, total_failures=30,
                   consecutive_failures=5, last_error_type='RATE_LIMITED', last_status=429,
                   flagged=True, quarantined_until=_NOW + timedelta(hours=21),
                   recent_events=[{'ts': _NOW.isoformat(), 'level': 'warning',
                                   'type': 'RATE_LIMITED', 'status': 429,
                                   'message': 'cryptoslate: returned HTTP 429'}])
    text = format_source_health_report(SourceHealthReport([flagged, _row('fxstreet')], []))
    assert '1 flagged' in text and '1 quarantined' in text
    assert 'FLAGGED(RATE_LIMITED)' in text and 'quarantined' in text
    assert '5!' in text                                   # consecutive marker on a flagged row
    assert 'RATE_LIMITED(429): cryptoslate: returned HTTP 429' in text   # recent problem line
    assert 'fxstreet' in text and 'ok' in text


def test_orphan_notice_lists_removed_sources():
    text = format_source_health_report(SourceHealthReport([_row('gone')], orphans=['gone']))
    assert 'may be deleted' in text
    assert '  gone' in text
    clean = format_source_health_report(SourceHealthReport([_row('here')], orphans=[]))
    assert '(none)' in clean


def test_recent_problems_capped_at_ten():
    events = [{'ts': (_NOW - timedelta(minutes=i)).isoformat(), 'level': 'error',
               'type': 'PARSE_ERROR', 'status': None, 'message': f'boom {i}'} for i in range(15)]
    row = _row('noisy', flagged=True, consecutive_failures=15, recent_events=events)
    text = format_source_health_report(SourceHealthReport([row], []))
    shown = [line for line in text.splitlines() if 'boom' in line]
    assert len(shown) == 10                               # newest 10 only (overview cap)
    assert 'boom 0' in text and 'boom 14' not in text     # newest kept, oldest dropped


# --- disabled sources are marked, never hidden ----------------------------------------

def test_disabled_source_is_marked_and_counted_not_hidden():
    # `enabled` is a config fact and source_health has no column for it, so an unmarked row shows
    # a switched-off feed's frozen last poll as a live `ok` — which is what it did before this.
    report = SourceHealthReport([_row('fxstreet', disabled=True), _row('forexlive')], [])
    text = format_source_health_report(report)

    assert 'ok [disabled]' in text                      # verdict kept, marker appended
    assert 'sources: 2 tracked · 1 disabled' in text
    assert 'fxstreet' in text and 'forexlive' in text   # still listed — the operator sees all
    assert report.disabled_count == 1


def test_disabled_source_keeps_its_health_verdict():
    # The health record is how the feed behaved while it *was* polled — precisely what the
    # decision to switch it back on rests on. So the marker must not swallow a flag.
    row = _row('cryptoslate', disabled=True, flagged=True, last_error_type='HTTP_ERROR',
               consecutive_failures=5, quarantined_until=_NOW + timedelta(hours=3))
    text = format_source_health_report(SourceHealthReport([row], []))

    assert 'FLAGGED(HTTP_ERROR)' in text and 'quarantined' in text
    assert '[disabled]' in text


def test_disabled_flagged_source_past_quarantine_never_claims_it_is_retrying():
    # Observed live: a disabled feed's quarantine elapses, its status flips to "retrying", and the
    # row freezes there forever — it is switched off, so no poll ever comes. "retrying" is the one
    # cell that is a claim about the *next* poll; for a disabled feed that claim is false.
    row = _row('fxstreet', disabled=True, flagged=True, last_error_type='HTTP_ERROR',
               consecutive_failures=5, quarantined_until=_NOW - timedelta(hours=1))  # elapsed
    text = format_source_health_report(SourceHealthReport([row], []))

    assert 'retrying' not in text                       # the false future-tense claim is gone
    assert 'FLAGGED(HTTP_ERROR) not polled [disabled]' in text   # verdict kept, honest verb


def test_enabled_flagged_source_past_quarantine_still_retries():
    # The complement: an *enabled* feed that cleared cool-off really will be polled again, so
    # "retrying" stays — the fix must not blunt the honest signal for feeds that are still live.
    row = _row('boe_news', flagged=True, last_error_type='HTTP_ERROR',
               consecutive_failures=5, quarantined_until=_NOW - timedelta(hours=1))
    text = format_source_health_report(SourceHealthReport([row], []))

    assert 'FLAGGED(HTTP_ERROR) retrying' in text
    assert '[disabled]' not in text


# --- feed doctor classifier (pure) ----------------------------------------------------

def test_classify_matches_the_source_taxonomy():
    assert classify_feed(429, None, True, 0) == 'RATE_LIMITED'   # the cryptoslate case
    assert classify_feed(503, None, True, 0) == 'HTTP_ERROR'
    assert classify_feed(None, 'SSLError: EOF', True, 0) == 'UNREACHABLE'
    assert classify_feed(200, None, True, 0) == 'PARSE_ERROR'    # bozo + no entries
    assert classify_feed(200, None, True, 10) == 'OK'            # bozo tolerated with entries
    assert classify_feed(200, None, False, 5) == 'OK'


def test_scan_suspicious_finds_bad_bytes_and_bare_amp():
    assert any('control byte' in f for f in _scan_suspicious(b'<rss>\x00bad</rss>'))
    assert any('bare &' in f for f in _scan_suspicious(b'<t>AT&T terms</t>'))
    assert _scan_suspicious(b'<rss>clean &amp; valid</rss>') == []


# --- the delivery barrier: parsing is not delivering (ISSUE_107) -----------------------

def test_classify_reports_a_2xx_that_carries_nothing():
    # binance's announcement RSS, measured 2026-08-25: HTTP 202 with an empty body. It parses
    # without complaint, so every other check calls it healthy. Threshold-free by construction.
    assert classify_feed(202, None, False, 0) == 'EMPTY'
    assert classify_feed(200, None, False, 0) == 'EMPTY'
    # A transport or HTTP failure outranks it — an unreachable feed's emptiness says nothing
    # about the feed.
    assert classify_feed(403, None, False, 0) == 'HTTP_ERROR'
    assert classify_feed(None, 'SSLError: EOF', False, 0) == 'UNREACHABLE'
    assert classify_feed(200, None, True, 0) == 'PARSE_ERROR'


def test_classify_reports_a_feed_that_parses_but_stopped_publishing():
    # blockworks, measured 2026-08-25: 200, 50 entries, newest 2026-01-07 — 5,520h old.
    assert classify_feed(200, None, False, 50, newest_age_hours=5520.0) == 'STALE'
    assert classify_feed(200, None, False, 50, newest_age_hours=12.0) == 'OK'
    # A feed may declare a slower rhythm: boc_press at 604h is a healthy press-release feed.
    assert classify_feed(200, None, False, 10, newest_age_hours=604.0) == 'STALE'
    assert classify_feed(200, None, False, 10, newest_age_hours=604.0,
                         max_age_hours=1440) == 'OK'
    # An undated feed is not a stale feed — plenty of valid RSS omits pubDate, and inventing a
    # verdict from a missing field would flag working sources for a formatting choice.
    assert classify_feed(200, None, False, 10, newest_age_hours=None) == 'OK'


def _diag(source_id, **kw):
    base = dict(url=f'https://{source_id}.test/feed', http_status=200, body_bytes=1000,
                entries=10, newest_age_hours=2.0)
    base.update(kw)
    return FeedDiagnosis(source_id=source_id, **base)


def test_the_doctor_counts_what_it_checked_and_names_the_gate():
    # "Nothing was reported" must be distinguishable from "nothing was checked" — a barrier
    # without a census is a barrier nobody can confirm ran.
    healthy = _diag('cointelegraph')
    fossil = _diag('blockworks', entries=50, newest_age_hours=5520.0, verdict='STALE')
    text = format_diagnoses([healthy, fossil])

    assert '2 probed · 1 OK · 1 STALE · 0 disabled' in text
    assert f'staleness gate: {DEFAULT_MAX_AGE_HOURS}h default' in text
    # The verdict carries its own basis inline, so it can be checked rather than believed.
    assert f'STALE (> {DEFAULT_MAX_AGE_HOURS}h · default)' in text


def test_the_doctor_names_a_feeds_own_threshold_where_it_declares_one():
    declared = _diag('boc_press', entries=10, newest_age_hours=604.0,
                     max_age_hours=1440, age_basis='declared')
    text = format_diagnoses([declared])
    assert '1 feed(s) declare their own (boc_press 1440h)' in text
    assert 'STALE' not in text          # 604h is inside its own declared 1440h


def test_the_doctor_explains_only_the_states_actually_present():
    # A legend that always lists everything is noise on a healthy fleet; one that appears exactly
    # when a state does is the state's own explanation.
    clean = format_diagnoses([_diag('cointelegraph')])
    assert 'STALE:' not in clean and 'EMPTY:' not in clean
    assert 'all feeds parse cleanly and carry recent items.' in clean

    empty = format_diagnoses([_diag('binance_ann', http_status=202, body_bytes=0, entries=0,
                                    newest_age_hours=None, verdict='EMPTY')])
    assert 'EMPTY: a 2xx that parses to zero entries' in empty
    assert 'STALE:' not in empty        # the state that did not occur is not explained


# --- the silence rule: reliability is not delivery (ISSUE_107) -------------------------

def test_a_feed_that_polls_perfectly_and_delivers_nothing_is_silent():
    # The failure mode every other number on this report misses: 100 % poll success, empty corpus.
    fossil = _row('blockworks', last_delivery_at=_NOW - timedelta(days=230), contributed=0)
    never = _row('binance_ann', last_delivery_at=None, contributed=0)
    delivering = _row('cointelegraph', last_delivery_at=_NOW - timedelta(minutes=4),
                      contributed=1204)
    assert fossil.silent is True
    assert never.silent is True          # nothing ever delivered must not hide behind a null
    assert delivering.silent is False

    text = format_source_health_report(SourceHealthReport([fossil, never, delivering], []))
    assert 'SILENT (quiet 230d > 168h · default)' in text
    assert 'SILENT (never delivered)' in text
    assert '2 silent' in text
    assert 'silence rule: nothing stored for longer than the feed is allowed to be quiet' in text
    assert 'reports.source_health.silence_days' in text


def test_a_low_volume_primary_feed_is_judged_by_its_own_allowance():
    # The case that corrected this rule while it was being built (2026-08-25): a flat 7-day window
    # called boc_press (25 days between press releases) and boe_news (14) silent while both were
    # perfectly healthy. Low volume IS what a central-bank press feed is, and the declaration that
    # says so is the same one the live probe judges staleness against.
    quiet_but_healthy = _row('boc_press', last_delivery_at=_NOW - timedelta(days=25),
                             contributed=0, allowance_hours=2160, allowance_basis='declared')
    assert quiet_but_healthy.silent is False
    # Without the declaration the very same feed trips — which is exactly why it is declared.
    assert _row('boc_press', last_delivery_at=_NOW - timedelta(days=25),
                contributed=0).silent is True

    text = format_source_health_report(
        SourceHealthReport([quiet_but_healthy], []))
    assert '1 feed(s) declare their own (boc_press 2160h)' in text
    assert 'SILENT' not in text
    assert '0 silent' in text


def test_silence_never_stacks_a_second_verdict_on_a_feed_that_already_has_one():
    # A disabled, quarantined or currently-failing feed contributes nothing *for a known reason*.
    # Reporting it silent as well would report one fault twice and bury the real cause.
    old = _NOW - timedelta(days=200)
    disabled = _row('fxstreet', last_delivery_at=old, contributed=0, disabled=True)
    quarantined = _row('theblock', last_delivery_at=old, contributed=0, flagged=True,
                       quarantined_until=_NOW + timedelta(hours=3))
    failing = _row('actionforex', last_delivery_at=old, contributed=0, consecutive_failures=2)
    assert [row.silent for row in (disabled, quarantined, failing)] == [False, False, False]


def test_an_unreadable_corpus_reports_the_rule_as_not_applied_never_as_silence():
    # On a fresh database `contributed` is 0 for everyone. Treating that as a verdict would flag
    # the entire fleet at once — "not measured" and "delivered nothing" are different answers.
    unknown = _row('cointelegraph', last_delivery_at=None, contributed=0,
                   contribution_known=False)
    assert unknown.silent is False

    text = format_source_health_report(
        SourceHealthReport([unknown], [], contribution_known=False))
    assert 'silence rule: NOT APPLIED' in text
    assert 'SILENT' not in text


def test_the_doctor_says_where_the_wait_went():
    # A 39-feed run is minutes sequentially and seconds pooled, and the cost is dominated by the
    # feeds that burn a timeout — not by the feed count. Without this line a slow run reads as a
    # hang, which is what it looked like on the server (ISSUE_107).
    walled = _diag('rbnz_press', http_status=403, entries=0, newest_age_hours=None,
                   verdict='HTTP_ERROR')
    text = format_diagnoses([_diag('cointelegraph'), walled],
                            elapsed_seconds=6.04, workers=8)
    assert 'took 6.0s · 4 requests, 8 at a time' in text
    assert '1 feed(s) burnt a timeout or were refused (rbnz_press)' in text
    # Sequential runs say so rather than claiming a pool.
    assert 'one at a time' in format_diagnoses([_diag('cointelegraph')], elapsed_seconds=1.2)
