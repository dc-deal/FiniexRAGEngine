"""Retrieval drift (ISSUE_55 groundwork) — the funnel per fingerprint and weekday.

Tests `aggregate_retrieval_drift` directly with synthetic store rows, so no DB is needed.

The central one is `test_a_cell_never_mixes_two_weekdays`: this report exists because a deploy on a
Saturday was read across a weekend and produced a wrong diagnosis twice. Pooling weekdays would make
it reproduce that error silently, which is the one failure mode worth pinning.
"""
from datetime import datetime, timezone

from finiexragengine.core.observability.reports.retrieval_drift_report import (
    aggregate_retrieval_drift,
    format_retrieval_drift_report,
)


def _row(pipeline, ts, fingerprint, *, symbols=1, in_window=24, floor_dropped=12, kept=10,
         best_distance=0.40, floor=0.55, deep_kept=None, prompt_version='4'):
    """One store row: (pipeline_id, ts, envelope) carrying `symbols` identical funnels."""
    funnel = {'in_window': in_window, 'floor_dropped': floor_dropped, 'kept': kept}
    if best_distance is not None:
        funnel['best_distance'] = best_distance
    if floor is not None:
        funnel['floor'] = floor
    if deep_kept is not None:
        funnel['deep_kept'] = deep_kept
    envelope = {
        'config_fingerprint': fingerprint,
        'prompt_version': prompt_version,
        'metadata': {'per_symbol_retrieval': {f'SYM{i}': dict(funnel) for i in range(symbols)}},
    }
    return (pipeline, ts, envelope)


def _at(day, hour=12):
    """A timestamp in August 2026 — 29th is a Saturday, 30th a Sunday, 31st a Monday."""
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


def test_a_cell_never_mixes_two_weekdays():
    """One fingerprint spanning a weekend is two rows, not one average.

    Saturday delivers 10 kept articles and Sunday 4. Pooled that reads as 7 — a number that
    describes neither day, and the shape in which a weekend gets attributed to a release.
    """
    rows = [
        _row('p', _at(29), 'aaaa', kept=10),      # Sat
        _row('p', _at(30), 'aaaa', kept=4),       # Sun
    ]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=1)

    assert len(report.rows) == 2
    assert [row.weekday_label for row in report.rows] == ['Sat', 'Sun']
    assert [row.kept_avg for row in report.rows] == [10.0, 4.0]
    # And the two are NOT compared against each other: they are different weekdays.
    assert report.deltas == []


def test_a_delta_only_pairs_fingerprints_inside_one_weekday():
    rows = [
        _row('p', _at(29, 10), 'before', floor_dropped=9),    # Sat, cut 37.5%
        _row('p', _at(29, 18), 'after', floor_dropped=15),    # Sat, cut 62.5%
        _row('p', _at(30, 10), 'after', floor_dropped=3),     # Sun — same fingerprint, other day
    ]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=1)

    assert len(report.deltas) == 1
    delta = report.deltas[0]
    assert (delta.weekday_label, delta.from_fingerprint, delta.to_fingerprint) == \
        ('Sat', 'before', 'after')
    assert delta.cut_pct_delta == 25.0            # 62.5 - 37.5, in percentage POINTS
    assert report.comparable_weekdays == 1


def test_the_delta_runs_later_minus_earlier_by_first_appearance():
    """Ordering is by when a fingerprint first appeared, not by its name.

    'zzzz' deployed first here; sorting by fingerprint string would invert the comparison and report
    an improvement as a regression.
    """
    rows = [
        _row('p', _at(29, 8), 'zzzz', kept=10),
        _row('p', _at(29, 20), 'aaaa', kept=6),
    ]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=1)

    delta = report.deltas[0]
    assert (delta.from_fingerprint, delta.to_fingerprint) == ('zzzz', 'aaaa')
    assert delta.kept_delta == -4.0


def test_cut_is_a_pooled_rate_not_a_mean_of_means():
    """Two passes with unequal candidate pools: 12/24 and 1/2 is 13/26, not (50% + 50%)/2 by luck.

    Made unambiguous by making the shares differ — 12/24 and 0/2 pool to 46.2 %, while averaging the
    two shares would give 25 %.
    """
    rows = [
        _row('p', _at(29, 8), 'aaaa', in_window=24, floor_dropped=12),
        _row('p', _at(29, 9), 'aaaa', in_window=2, floor_dropped=0),
    ]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=1)

    assert round(report.rows[0].cut_pct, 1) == 46.2


def test_a_pass_without_a_distance_does_not_drag_the_average():
    """`best_distance` is null when the window held nothing — a missing sample, not a zero."""
    rows = [
        _row('p', _at(29, 8), 'aaaa', best_distance=0.40),
        _row('p', _at(29, 9), 'aaaa', best_distance=None),
    ]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=1)

    row = report.rows[0]
    assert row.best_distance_avg == 0.40      # not 0.20
    assert row.symbol_passes == 2             # the pass still counts everywhere else


def test_an_envelope_from_before_the_deep_tier_reads_as_zero_not_as_missing():
    rows = [_row('p', _at(29), 'aaaa', deep_kept=None)]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=1)

    assert report.rows[0].deep_kept_avg == 0.0


def test_a_thin_cell_is_marked_and_kept():
    """Dropping it would hide which fingerprint ran that day, which is evidence in itself."""
    rows = [_row('p', _at(29), 'aaaa', symbols=3)]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=40)

    assert report.rows[0].symbol_passes == 3
    assert report.rows[0].thin
    assert report.thin_rows == 1


def test_a_floor_retuned_inside_a_cell_is_flagged_rather_than_averaged_over():
    rows = [
        _row('p', _at(29, 8), 'aaaa', floor=0.55),
        _row('p', _at(29, 9), 'aaaa', floor=0.62),
    ]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=1)

    assert report.rows[0].floors == (0.55, 0.62)
    assert report.rows[0].floor_conflict


def test_an_envelope_without_a_fingerprint_is_grouped_not_dropped():
    """Pre-ISSUE_85 envelopes carry none. They are still passes that happened."""
    rows = [('p', _at(29), {'metadata': {'per_symbol_retrieval': {'S': {'in_window': 24}}}})]
    report = aggregate_retrieval_drift(rows, '14d', min_passes=1)

    assert report.rows[0].config_fingerprint == '(unstamped)'


def test_the_reading_separates_a_wider_spread_from_a_corpus_that_moved():
    """The two findings this report exists to tell apart, and the pair is what tells them apart."""
    flat = [
        _row('p', _at(29, 8), 'before', floor_dropped=9, best_distance=0.400),
        _row('p', _at(29, 9), 'after', floor_dropped=15, best_distance=0.402),
    ]
    moved = [
        _row('p', _at(29, 8), 'before', floor_dropped=9, best_distance=0.400),
        _row('p', _at(29, 9), 'after', floor_dropped=15, best_distance=0.450),
    ]

    assert 'wider spread' in aggregate_retrieval_drift(flat, '14d', 1).deltas[0].reading
    assert 'moved away' in aggregate_retrieval_drift(moved, '14d', 1).deltas[0].reading


def test_a_rising_cut_beside_a_CLOSER_nearest_is_its_own_reading():
    """The sharpening signature — and the case the first version got backwards.

    Testing `abs(best_distance_delta)` folded this into the "nearest got further" branch and
    reported the corpus as moving *away* when it had moved *toward*. It mislabelled the exact
    comparison this report was built for: production `forex_macro_sentiment` on Tuesday across the
    ISSUE_112 boundary, cut +10.4pp with `best` improving 0.411 → 0.395.
    """
    rows = [
        _row('p', _at(29, 8), 'before', floor_dropped=11, best_distance=0.411),
        _row('p', _at(29, 9), 'after', floor_dropped=14, best_distance=0.395),
    ]
    delta = aggregate_retrieval_drift(rows, '14d', 1).deltas[0]

    assert delta.cut_pct_delta > 0 and delta.best_distance_delta < 0
    assert 'CLOSER' in delta.reading and 'sharpened' in delta.reading
    assert 'moved away' not in delta.reading


def test_a_falling_cut_beside_a_FURTHER_nearest_is_its_own_reading():
    """The other mixed case, pinned for the same reason: a sign, not a magnitude, selects it."""
    rows = [
        _row('p', _at(29, 8), 'before', floor_dropped=15, best_distance=0.400),
        _row('p', _at(29, 9), 'after', floor_dropped=9, best_distance=0.450),
    ]
    delta = aggregate_retrieval_drift(rows, '14d', 1).deltas[0]

    assert 'FURTHER' in delta.reading
    assert 'moved toward' not in delta.reading


def test_a_deep_tier_switching_on_refuses_to_be_read_as_a_corpus_change():
    """The production case: the pool doubles, both rate columns move, and neither means anything.

    `crypto_sentiment` Tuesday 2026-09-01 — pool 24.0 -> 48.0, `cut%` 11.7 -> 46.1, `kept` 11.5 ->
    11.3. The second tier reaches a week back, so its candidates are older, further and mostly cut;
    the prompt was unchanged. Naming a cause from a rate whose basis moved is the same error as
    reading a deploy across a weekend.
    """
    rows = [
        _row('p', _at(29, 8), 'before', in_window=24, floor_dropped=3, best_distance=0.487),
        _row('p', _at(29, 9), 'after', in_window=48, floor_dropped=22, best_distance=0.525,
             deep_kept=1),
    ]
    delta = aggregate_retrieval_drift(rows, '14d', 1).deltas[0]

    assert delta.basis_changed
    assert 'not comparable' in delta.reading
    assert 'moved away' not in delta.reading


def test_the_deep_tier_is_detected_even_when_it_kept_nothing():
    """Presence, not contribution: a tier that fetched and had everything cut still moved the pool.

    The pool size alone would catch this one too; the point is that the tier's presence is checked
    independently, so a tier that neither grew the pool measurably nor kept anything cannot slip
    through as an ordinary step.
    """
    rows = [
        _row('p', _at(29, 8), 'before', deep_kept=2),
        _row('p', _at(29, 9), 'after', deep_kept=0),
    ]
    delta = aggregate_retrieval_drift(rows, '14d', 1).deltas[0]

    assert delta.deep_changed and delta.basis_changed


def test_ordinary_weekday_variation_is_still_read_normally():
    """The guard must not swallow real findings — a thin window is a finding, not a basis change.

    Production's widest same-basis step was a thin Sunday at 19.6 vs 14.5 candidates (26 %), well
    inside the margin, and it still reads as a cut/distance pair.
    """
    rows = [
        _row('p', _at(29, 8), 'before', in_window=20, floor_dropped=17, best_distance=0.601),
        _row('p', _at(29, 9), 'after', in_window=15, floor_dropped=13, best_distance=0.619),
    ]
    delta = aggregate_retrieval_drift(rows, '14d', 1).deltas[0]

    assert not delta.basis_changed
    assert 'not comparable' not in delta.reading


def test_an_empty_window_renders_a_statement_rather_than_an_empty_table():
    report = aggregate_retrieval_drift([], '14d', min_passes=40)
    rendered = format_retrieval_drift_report(report)

    assert 'nothing to compare' in rendered


def test_the_render_names_the_missing_comparison_when_no_weekday_has_a_pair():
    rows = [_row('p', _at(29), 'aaaa', symbols=50)]
    rendered = format_retrieval_drift_report(aggregate_retrieval_drift(rows, '14d', 40))

    assert 'no weekday carries two fingerprints yet' in rendered
