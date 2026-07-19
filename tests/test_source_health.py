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
from finiexragengine.core.sources.feed_doctor import _scan_suspicious, classify_feed
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
