"""Breaking state timeline (ISSUE_82) — the on/off series behind the episode count, DB-free.

Tests `_aggregate_timeline` directly with synthetic store rows, like the funnel report's suite.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.breaking_timeline_report import (
    _aggregate_timeline,
    format_breaking_timeline_report,
)
from finiexragengine.core.pipeline.breaking_episode_rule import BreakingEpisodeRule

_T0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _result(symbol, urgency, threshold=0.8, basis='llm', base=None):
    result = {'symbol': symbol, 'urgency': urgency, 'is_breaking': urgency >= threshold,
              'signal': 'BUY', 'reasoning': '', 'basis': basis, 'sources': []}
    if base is not None:
        result['base_currency'] = base
    return result


def _row(pipeline, ts, *, symbol='XRPUSD', urgency=0.8, threshold=0.8, basis='llm', base=None):
    """One stored envelope; `is_breaking` derives from the threshold that was in force then."""
    return (pipeline, {'timestamp': ts.isoformat(),
                       'result': [_result(symbol, urgency, threshold, basis, base)]})


def _series(urgencies, *, pipeline='crypto_sentiment', symbol='XRPUSD'):
    return [_row(pipeline, _T0 + timedelta(minutes=10 * i), symbol=symbol, urgency=u)
            for i, u in enumerate(urgencies)]


def _window(count):
    """The window a `_series` of `count` 10-minute passes spans."""
    return {'since': _T0, 'until': _T0 + timedelta(minutes=10 * (count - 1))}


def _strip_of(report, label, width=200):
    out = format_breaking_timeline_report(report, width=width)
    line = next(line for line in out.splitlines() if line.startswith(label))
    return line.split()[-1]


# --- the measured case -------------------------------------------------------------------------

def test_the_measured_xrpusd_sequence_reports_the_flips_and_one_episode():
    """2026-08-17, the real series — this report exists to make both numbers visible at once.

    The flips are the model's noise and must NOT change with the grouping rule; the episode count
    is what the rule made of them, and is the number ISSUE_82 set out to correct.
    """
    measured = [0.8, 0.8, 0.8, 0.8, 0.7, 0.7, 0.7, 0.8, 0.7, 0.8, 0.7, 0.8, 0.6, 0.8]
    report = _aggregate_timeline(_series(measured), '7d', '', {}, **_window(len(measured)))
    row = report.rows[0]
    assert row.label() == 'XRPUSD'
    assert row.passes == 14 and row.breaking_passes == 8 and row.mechanical == 0
    assert row.flips == 8                       # threshold crossings, unchanged by any rule
    assert row.episodes == 1                    # was 2 under the pre-ISSUE_82 grouping
    # One cell per pass at this width — the comb this issue is about.
    assert _strip_of(report, 'XRPUSD')[:14] == '####...#.#.#_#'


def test_the_series_marks_breaking_held_and_below_apart():
    report = _aggregate_timeline(_series([0.8, 0.7, 0.3]), '7d', '', {}, **_window(3))
    assert _strip_of(report, 'XRPUSD')[:3] == '#._'


def test_the_exit_gate_comes_from_the_pipelines_own_rule():
    # A pipeline configured without hysteresis renders the same passes differently — the strip is
    # the rule's view, not a second opinion about it.
    rules = {'crypto_sentiment': BreakingEpisodeRule(exit_threshold=0.8,
                                                     gap=timedelta(minutes=45))}
    report = _aggregate_timeline(_series([0.8, 0.7]), '7d', '', rules, **_window(2))
    assert _strip_of(report, 'XRPUSD')[:2] == '#_'


# --- what must stay visible ---------------------------------------------------------------------

def test_mechanical_holds_are_counted_but_never_scored():
    """`basis='no_data'` rows never reached the model, so they are not evidence about its
    stability — but dropping them made a symbol vanish, which read as 'not configured'."""
    rows = _series([0.8]) + [_row('crypto_sentiment', _T0 + timedelta(minutes=10),
                                  urgency=0.0, basis='no_data')]
    report = _aggregate_timeline(rows, '7d', '', {}, **_window(2))
    row = report.rows[0]
    assert row.passes == 1 and row.mechanical == 1
    # Its own cell, not `~`: the engine ran, this symbol simply had nothing to read. Rendering it
    # as absent made a calm symbol look like an outage (production, 2026-08-17).
    assert _strip_of(report, 'XRPUSD')[:2] == '#-'


def test_passes_and_mechanical_sum_to_the_envelope_count_on_a_merged_row():
    """Both columns count per analysis unit per pass, so their sum is readable on every row.

    Counting `mechanical` per *result* made the two columns different units: a merged FX row
    reported more mechanical holds than the pipeline had passes.
    """
    rows = []
    for index in range(4):
        ts = _T0 + timedelta(minutes=10 * index)
        scored = index % 2 == 0                       # alternate: unit scored / unit not scored
        rows.append(('forex_macro_sentiment', {'timestamp': ts.isoformat(), 'result': [
            _result('USDJPY', 0.2, basis='llm' if scored else 'no_data', base='USD'),
            _result('USDCAD', 0.1, basis='no_data', base='USD'),
            _result('USDCHF', 0.1, basis='no_data', base='USD')]}))
    row = _aggregate_timeline(rows, '7d', '', {}, **_window(4)).rows[0]
    assert row.label() == 'USDJPY/USDCAD/USDCHF'
    assert row.passes == 2 and row.mechanical == 2    # not 2 and 10
    assert row.passes + row.mechanical == 4


def test_a_symbol_that_never_broke_still_gets_a_row():
    # The operator's question, as a regression: "no line" used to mean never-broke, never-scored
    # or not-configured, and nothing distinguished them.
    rows = _series([0.2, 0.3], symbol='LTCUSD') + _series([0.8], symbol='XRPUSD')
    report = _aggregate_timeline(rows, '7d', '', {}, **_window(2))
    assert [row.label() for row in report.rows] == ['XRPUSD', 'LTCUSD']   # breaking first
    quiet = report.rows[1]
    assert quiet.breaking_passes == 0 and quiet.episodes == 0 and quiet.passes == 2


def test_a_symbol_scored_only_mechanically_is_visible_as_such():
    rows = [_row('forex_macro_sentiment', _T0 + timedelta(minutes=10 * i),
                 symbol='NZDUSD', urgency=0.0, basis='no_data') for i in range(3)]
    report = _aggregate_timeline(rows, '7d', '', {}, **_window(3))
    row = report.rows[0]
    assert row.label() == 'NZDUSD'
    assert row.passes == 0 and row.mechanical == 3 and row.breaking_passes == 0
    assert set(_strip_of(report, 'NZDUSD')) == {'-'}, 'never scored is not the same as no pass'


def test_the_footer_counts_analysis_units_not_tickers():
    rows = _series([0.2], symbol='LTCUSD') + _series([0.8], symbol='XRPUSD')
    out = format_breaking_timeline_report(_aggregate_timeline(rows, '7d', '', {}, **_window(1)),
                                          width=140)
    assert 'across 1 of 2 analysis unit(s)' in out


# --- grouping ------------------------------------------------------------------------------------

def test_fanned_legs_are_one_row_and_are_not_double_counted():
    """ISSUE_70: ETHUSD/ETHEUR are one analysis. Two rows put every episode on one leg and a bare
    `0` on the other, and doubled both totals — the first thing production made obvious."""
    rows = []
    for index, urgency in enumerate([0.9, 0.9, 0.3]):
        ts = _T0 + timedelta(minutes=10 * index)
        rows.append(('crypto_sentiment', {'timestamp': ts.isoformat(), 'result': [
            _result('ETHUSD', urgency, base='ETH'), _result('ETHEUR', urgency, base='ETH')]}))
    report = _aggregate_timeline(rows, '7d', '', {}, **_window(3))
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.label() == 'ETHUSD/ETHEUR'
    assert row.passes == 3 and row.breaking_passes == 2      # not 6 and 4
    assert row.flips == 1 and row.episodes == 1              # not 2 and 1-plus-a-zero-twin


def test_a_symbol_filter_narrows_the_output_without_changing_the_grouping():
    """The filter must not fabricate gaps: the rule sees every pass either way."""
    rows = []
    for index, urgency in enumerate([0.8, 0.7, 0.8]):
        ts = _T0 + timedelta(minutes=10 * index)
        rows.append(_row('crypto_sentiment', ts, symbol='XRPUSD', urgency=urgency))
        rows.append(_row('crypto_sentiment', ts, symbol='BTCUSD', urgency=0.9))
    unfiltered = _aggregate_timeline(rows, '7d', '', {}, **_window(3))
    filtered = _aggregate_timeline(rows, '7d', 'XRPUSD', {}, **_window(3))
    assert [row.label() for row in filtered.rows] == ['XRPUSD']
    xrp = next(row for row in unfiltered.rows if row.label() == 'XRPUSD')
    assert filtered.rows[0].episodes == xrp.episodes == 1


# --- the strip spans the window ------------------------------------------------------------------

def test_a_long_window_is_bucketed_not_truncated():
    """The production defect, as a regression.

    1079 passes cut to the ~52 cells a console affords showed the last nine hours under a header
    that said 'last 7d'. Bucketing keeps the whole window on screen: a breaking burst at the START
    of a long window must still be visible at the left edge.
    """
    urgencies = [0.9, 0.9, 0.9] + [0.1] * 400
    report = _aggregate_timeline(_series(urgencies), '7d', '', {}, **_window(len(urgencies)))
    strip = _strip_of(report, 'XRPUSD', width=100)
    assert len(strip) < len(urgencies), 'the strip is condensed, not one cell per pass'
    assert strip[0] == '#', 'the opening burst must survive bucketing'
    assert '…' not in strip and strip.endswith('_')


def test_a_bucket_with_no_pass_reads_as_an_outage():
    # An engine that stopped producing must not look like a calm market. Realistic shape: a run of
    # passes on the normal cadence, a silence far longer than it, then the cadence resumes.
    rows = _series([0.9] * 6)
    resumed = _T0 + timedelta(hours=10)
    rows += [_row('crypto_sentiment', resumed + timedelta(minutes=10 * i), urgency=0.9)
             for i in range(6)]
    report = _aggregate_timeline(rows, '7d', '', {},
                                 since=_T0, until=resumed + timedelta(minutes=50))
    strip = _strip_of(report, 'XRPUSD', width=200)
    assert strip[0] == '#' and strip[-1] == '#'
    assert '~' in strip, 'the ten-hour silence must render as absent, not as below'


def test_one_breaking_pass_in_a_bucket_wins_over_its_quiet_neighbours():
    urgencies = [0.1] * 20 + [0.9] + [0.1] * 20
    report = _aggregate_timeline(_series(urgencies), '7d', '', {}, **_window(len(urgencies)))
    assert '#' in _strip_of(report, 'XRPUSD', width=90)


# --- rendering -------------------------------------------------------------------------------------

def test_format_renders_the_house_pattern_and_both_totals():
    report = _aggregate_timeline(_series([0.8, 0.7, 0.8]), '7d', '', {}, **_window(3))
    out = format_breaking_timeline_report(report, width=140)
    assert 'Breaking state timeline' in out
    assert 'window: last 7d' in out
    assert 'XRPUSD' in out
    assert 'verdict flips → 1 episodes' in out          # the two numbers side by side


def test_format_survives_an_empty_window():
    out = format_breaking_timeline_report(_aggregate_timeline([], '7d', '', {}), width=80)
    assert '(no passes in the window)' in out


def test_the_render_never_overruns_the_console():
    report = _aggregate_timeline(_series([0.8] * 400), '7d', '', {}, **_window(400))
    out = format_breaking_timeline_report(report, width=100)
    assert max(len(line) for line in out.splitlines()) <= 100


def test_the_header_names_the_rule_each_pipeline_was_grouped_with():
    """A report that re-derives the archive at read time has to say under which rule.

    Without it, two runs of the same command over the same data can differ and nothing on the page
    explains why — the `[OVERRIDE]` startup line only appears when an override happens to exist.
    """
    rows = _series([0.8]) + [_row('forex_macro_sentiment', _T0, symbol='GBPUSD', urgency=0.8)]
    rules = {'crypto_sentiment': BreakingEpisodeRule(exit_threshold=0.7,
                                                     gap=timedelta(minutes=150))}
    out = format_breaking_timeline_report(
        _aggregate_timeline(rows, '7d', '', rules, **_window(1)), width=140)
    assert 'episode rule (read-time):' in out
    assert 'crypto_sentiment' in out and 'gap 150m' in out
    # The pipeline without an override falls back to the schema defaults, and says so rather than
    # silently borrowing the other one's numbers.
    assert 'gap 150m' in out


def test_a_single_pipeline_renders_the_rule_inline():
    out = format_breaking_timeline_report(
        _aggregate_timeline(_series([0.8]), '7d', '', {}, **_window(1)), width=140)
    assert 'episode rule (read-time): crypto_sentiment hold ≥0.70 · gap 150m' in out
