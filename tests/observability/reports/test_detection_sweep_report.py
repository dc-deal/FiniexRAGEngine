"""Detection sweep (ISSUE_106) — the grid, the evidence, and the verdict wording.

Pure rendering and pure counting: the DB half is one self-join and is exercised by running the CLI,
not by seeding a corpus. What must not regress is the *reading* of the numbers — a sweep that
reports counts without saying which of them is intra-feed duplication invites exactly the wrong
conclusion, which is the mistake this report exists to prevent.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.detection_sweep_report import (
    ClusterExample,
    DetectionSweepReport,
    SweepCell,
    _story_counts,
    format_detection_sweep_report,
)

_T0 = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _report(cells, examples=(), seeds=200, live=0.85) -> DetectionSweepReport:
    return DetectionSweepReport(
        source_set_id='crypto_news', since_label='30d', seeds=seeds, window_minutes=60,
        mid_cluster_size=3, high_cluster_size=5, live_similarity=live,
        cells=list(cells), examples=list(examples))


def _grid(**per_detector) -> list:
    """cells for one similarity, e.g. _grid(articles=(9, 1), feeds=(2, 0), story=(0, 0))."""
    return [SweepCell(detector=d, similarity=0.65, reaches_mid=mid, reaches_high=high)
            for d, (mid, high) in per_detector.items()]


def test_an_inert_live_configuration_is_named_as_inert_not_as_strict():
    # The finding this report was built for: at the running similarity the path reaches NEITHER
    # tier on any seed. "Strict" would suggest tuning; "inert" is what the measurement says.
    cells = [SweepCell('articles', 0.85, 0, 0), SweepCell('feeds', 0.85, 0, 0),
             SweepCell('story', 0.85, 0, 0)]
    text = format_detection_sweep_report(_report(cells))

    assert 'reaches neither tier on any of the 200 seeds' in text
    assert 'inert, not merely strict' in text
    assert '<- live' in text                       # the running row is marked in the grid


def test_a_live_configuration_that_does_fire_gets_no_inert_verdict():
    cells = [SweepCell('articles', 0.85, 12, 3), SweepCell('feeds', 0.85, 4, 1),
             SweepCell('story', 0.85, 2, 0)]
    assert 'inert' not in format_detection_sweep_report(_report(cells))


def test_a_single_feed_neighbourhood_is_called_out_as_duplication():
    # cryptonews' "XRP Price Prediction:" series and actionforex's "Daily Outlook" are the measured
    # cases: many members, ONE feed. Counting them as a cluster is the failure mode.
    boilerplate = ClusterExample(similarity=0.65, seed_source_id='cryptonews',
                                 seed_title='XRP Price Prediction: Why Is XRP Fluctuating?',
                                 members=5, distinct_feeds=1,
                                 peers=[('cryptonews', 'XRP Price Prediction: Momentum', 0.236)])
    real = ClusterExample(similarity=0.75, seed_source_id='decrypt',
                          seed_title='Strategy Raises $2B Selling MSTR Stock',
                          members=3, distinct_feeds=3,
                          peers=[('coindesk', 'Strategy raises $2 billion', 0.170),
                                 ('theblock', 'Strategy sells $2 billion', 0.205)])
    assert boilerplate.is_single_feed is True
    assert real.is_single_feed is False

    text = format_detection_sweep_report(_report(_grid(articles=(9, 1), feeds=(2, 0), story=(0, 0)),
                                                 examples=[boilerplate, real]))
    assert 'ONE FEED, one template' in text
    assert text.count('ONE FEED') == 1              # only the single-feed one is flagged
    assert '1 of 2 shown neighbourhoods are a single feed' in text
    assert 'admits those first' in text             # the actionable half of the finding
    assert 'coindesk' in text and '0.170' in text   # the real story keeps its evidence


def test_the_render_never_lets_the_three_columns_pass_as_one_measure():
    # STORY is a group size, the other two are neighbourhood sizes. Comparable in intent, not in
    # construction — a reader who assumes otherwise draws a wrong conclusion from a true table.
    text = format_detection_sweep_report(_report(_grid(articles=(9, 1), feeds=(2, 0), story=(4, 1))))
    assert 'group size' in text and 'neighbourhood sizes' in text
    assert 'the gap between the two columns IS intra-feed duplication' in text


def test_an_empty_window_says_so_rather_than_rendering_a_grid_of_zeros():
    text = format_detection_sweep_report(_report([], seeds=0))
    assert 'no articles in the window' in text
    assert 'inert' not in text                      # nothing measured is not a verdict


def test_the_story_measure_is_tested_not_assumed():
    """The hypothesis is that IDF discounts a template's shared words. It holds only so far.

    With per-item bodies — which is what actionforex and cryptonews actually publish — three
    outlets on one event group larger than a template series. With a BYTE-IDENTICAL body it does
    not, and the sweep is supposed to reveal that rather than paper over it: no text measure
    separates two documents that differ by one token in ten.
    """
    def rows(a_bodies):
        base = [('a1', 'actionforex', 'EUR/USD Daily Outlook', a_bodies[0], _T0),
                ('a2', 'actionforex', 'EUR/AUD Daily Outlook', a_bodies[1],
                 _T0 + timedelta(minutes=1)),
                ('a3', 'actionforex', 'EUR/CHF Daily Outlook', a_bodies[2],
                 _T0 + timedelta(minutes=2))]
        event = [('b1', 'decrypt', 'Strategy raises $2B selling MSTR stock',
                  'Strategy sold two billion dollars of MSTR shares this week, buying no bitcoin.',
                  _T0 + timedelta(minutes=3)),
                 ('b2', 'coindesk', 'Strategy raises $2 billion through MSTR sales',
                  'The company raised two billion dollars selling MSTR shares and bought no bitcoin.',
                  _T0 + timedelta(minutes=4)),
                 ('b3', 'theblock', 'Strategy sells $2 billion in MSTR shares',
                  'Two billion dollars of MSTR stock sold by Strategy, with no bitcoin purchase.',
                  _T0 + timedelta(minutes=5))]
        return base + event

    # Realistic: each pair carries its own commentary, as the live feeds do.
    distinct = rows(['Euro dollar support at 1.08, resistance 1.09, momentum fading.',
                     'Euro aussie holds 1.64 with the bias turning lower on the daily.',
                     'Euro swiss grinds toward 0.94 as the range compresses further.'])
    counts = _story_counts(distinct, {'a1', 'b1'}, (0.30,), window_minutes=60)
    assert counts['b1'][0.30] >= 3, counts
    assert counts['a1'][0.30] < counts['b1'][0.30], counts

    # Degenerate: one body reused verbatim. The measure cannot separate them, and must not pretend
    # to — this is the limit the sweep exists to expose.
    identical = rows(['Daily technical outlook for the pair.'] * 3)
    same = _story_counts(identical, {'a1'}, (0.30,), window_minutes=60)
    assert same['a1'][0.30] >= 3, same


def test_a_thin_sample_refuses_to_read_as_a_finding():
    # 164 seeds over one day answered "0 cross-feed clusters". That is "not in these hours", not
    # "does not happen", and the render must not let the two look alike.
    report = _report([SweepCell('feeds', 0.55, 0, 0)])
    report.oldest_seed = _T0
    report.newest_seed = _T0 + timedelta(hours=20)
    text = format_detection_sweep_report(report)
    assert 'sample spans 0.8 days' in text
    assert 'a THIN sample' in text
    assert 'not in these hours' in text

    report.newest_seed = _T0 + timedelta(days=21)
    assert 'THIN sample' not in format_detection_sweep_report(report)


def test_the_sample_reports_which_text_treatment_it_measured():
    # 44 % of the corpus still stores raw HTML; two articles sharing a feed's markup are similar
    # BECAUSE OF THE MARKUP. A grid read off a mixed sample measures the wrong thing.
    mixed = _report([SweepCell('articles', 0.65, 9, 1)])
    mixed.normalizers = {'v1': 164, '(raw — pre-ISSUE_112)': 36}
    text = format_detection_sweep_report(mixed)
    assert mixed.mixed_text_treatments is True
    assert 'MIXED text treatments' in text and 'BECAUSE OF THE MARKUP' in text

    clean = _report([SweepCell('articles', 0.65, 9, 1)])
    clean.normalizers = {'v1': 164}
    assert 'MIXED' not in format_detection_sweep_report(clean)
