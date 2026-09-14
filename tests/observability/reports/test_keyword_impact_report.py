"""Keyword impact (ISSUE_124) — flags joined to the envelopes they woke, per term.

Against the DB-free core (`_aggregate`) and the renderer: the SQL either side of it is two plain
SELECTs, while everything that can be wrong while looking right lives in the attribution.

What is asserted here is exactly that:

- **citation is the proof**, so a reaction time exists only where the woken envelope cited the
  flagged article — a reaction without it is arithmetic about an article nobody read;
- **a pass already running cannot have been woken**, and a breaking pass a quarter of an hour later
  belongs to another story;
- **NULL is not zero**: a flag whose terms were never recorded is `unrecorded`, never a term that
  fired nothing;
- **the baseline is printed**, and its absence is said out loud rather than rendered as `0.00`.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from finiexragengine.core.observability.reports.corpus_text_report import KeywordSet
from finiexragengine.core.observability.reports.keyword_impact_report import (
    KeywordImpactReport,
    _aggregate,
    _Flag,
    _Pass,
    _read_pass,
    format_keyword_impact_report,
)

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
_SET = KeywordSet(source_set_id='forex_news',
                  keywords=('fomc statement', 'intervention', 'monetary policy assessment'),
                  keyword_source_weight=0.9,
                  weights={'fed_press': 1.0, 'forexlive': 1.0})


def _report() -> KeywordImpactReport:
    return KeywordImpactReport(source_set_id=_SET.source_set_id, since_label='30d',
                               pipelines=['forex_macro_sentiment'],
                               vocabulary=list(_SET.keywords))


def _flag(term: str, *, at_minutes: float = 0.0, article: str = 'a1', source: str = 'fed_press',
          title: str = 'Federal Reserve issues FOMC statement',
          age_minutes: Optional[float] = 5.0) -> _Flag:
    at = _NOW + timedelta(minutes=at_minutes)
    return _Flag(article_id=article, title=title, source_id=source, at=at,
                 published_at=None if age_minutes is None else at - timedelta(minutes=age_minutes),
                 terms=(term,) if term else ())


def _pass(*, at_minutes: float, breaking: bool = True, cited=(), urgency: float = 0.5,
          confirmed: bool = False) -> _Pass:
    return _Pass(pipeline_id='forex_macro_sentiment', at=_NOW + timedelta(minutes=at_minutes),
                 breaking=breaking, cited=set(cited), urgency=urgency, confirmed=confirmed)


def test_a_cited_flag_reports_its_reaction_and_an_uncited_one_reports_a_blank():
    """The column and the timing are one fact: the envelope read what the flag raised.

    An uncited flag still woke a pass — that is the waste the report is for — but it has no
    reaction, and a zero there would read as "instant", the opposite of what happened.
    """
    report = _aggregate(_report(),
                        [_flag('fomc statement', article='a1'),
                         _flag('intervention', article='a2', at_minutes=30)],
                        [_pass(at_minutes=8, cited=['a1'], confirmed=True),
                         _pass(at_minutes=31, cited=['other'])])

    cited, uncited = report.terms[0], report.terms[1]
    assert (cited.term, cited.woke, cited.cited, cited.confirmed) == ('fomc statement', 1, 1, 1)
    assert cited.reaction_s == 8 * 60
    assert (uncited.term, uncited.woke, uncited.cited) == ('intervention', 1, 0)
    assert uncited.reaction_s is None and uncited.unread == 1


def test_a_pass_that_was_already_running_is_not_the_flags_wake():
    """The envelope predates the flag, so it cannot have been woken by it — nor cite it."""
    report = _aggregate(_report(), [_flag('fomc statement', at_minutes=10)],
                        [_pass(at_minutes=2, cited=['a1'])])

    assert report.terms[0].woke == 0 and report.terms[0].cited == 0


def test_a_breaking_pass_long_after_the_flag_belongs_to_another_story():
    """The bus publishes on the flagging pass, so the wake is seconds away, not a quarter hour.

    Without the bound, any later breaking episode would be credited to whichever term fired before
    it — and the report would invent savings out of unrelated stories.
    """
    late = _aggregate(_report(), [_flag('fomc statement')], [_pass(at_minutes=16, cited=['a1'])])
    inside = _aggregate(_report(), [_flag('fomc statement')], [_pass(at_minutes=14, cited=['a1'])])

    assert late.terms[0].woke == 0
    assert inside.terms[0].woke == 1 and inside.terms[0].cited == 1


def test_two_flags_from_one_pass_share_the_single_envelope_they_woke():
    """One wake for two articles is one wake — the envelope is shared, not counted twice."""
    woken = _pass(at_minutes=5, cited=['a1', 'a2'])
    report = _aggregate(_report(),
                        [_flag('fomc statement', article='a1'),
                         _flag('intervention', article='a2')],
                        [woken])

    assert [(row.term, row.woke, row.cited) for row in report.terms] == [
        ('fomc statement', 1, 1), ('intervention', 1, 1)]
    assert report.flags == 2 and report.flagged_articles == 2


def test_one_article_matching_two_terms_counts_for_both_and_the_header_says_so():
    """Attribution per term is the point; the double count is made visible, never hidden."""
    both = _Flag(article_id='a1', title='Fed statement amid intervention talk',
                 source_id='fed_press', at=_NOW, terms=('fomc statement', 'intervention'))
    report = _aggregate(_report(), [both], [_pass(at_minutes=3, cited=['a1'])])

    assert {row.term: row.flags for row in report.terms} == {'fomc statement': 1,
                                                             'intervention': 1}
    assert report.flags == 1 and report.flagged_articles == 1


def test_a_flag_without_recorded_terms_is_unrecorded_rather_than_a_silent_term():
    """NULL `detection_keywords` means the vocabulary was never written down for that flag.

    Folding it into a term would credit a word that decided nothing; dropping it silently would
    make the header's flag count disagree with the rows below it.
    """
    report = _aggregate(_report(), [_flag('', article='a9')], [_pass(at_minutes=1)])

    assert report.terms == [] and report.unrecorded == 1 and report.flags == 1
    assert 'carry no vocabulary' in format_keyword_impact_report(report, width=110)


def test_the_delta_is_against_the_scheduled_passes_of_the_same_window():
    """Urgency alone says nothing — what matters is the distance to the passes around it."""
    report = _aggregate(_report(), [_flag('fomc statement')],
                        [_pass(at_minutes=5, cited=['a1'], urgency=0.8),
                         _pass(at_minutes=20, breaking=False, urgency=0.3),
                         _pass(at_minutes=40, breaking=False, urgency=0.5)])

    assert report.baseline_urgency == 0.4 and report.woken_urgency == 0.8
    assert report.delta(report.terms[0]) == 0.4


def test_a_window_without_a_scheduled_pass_says_the_baseline_is_unavailable():
    """A missing baseline is not a baseline of zero, and the delta column says so too."""
    report = _aggregate(_report(), [_flag('fomc statement')],
                        [_pass(at_minutes=5, cited=['a1'], urgency=0.8)])
    text = format_keyword_impact_report(report, width=110)

    assert report.baseline_urgency is None and report.delta(report.terms[0]) is None
    assert 'unavailable — no scheduled pass in this window' in text


def test_reach_counts_citation_anywhere_and_stays_out_of_the_cited_column():
    """An article the NEXT scheduled pass picked up was read — but not because of the flag."""
    report = _aggregate(_report(), [_flag('intervention', article='a2')],
                        [_pass(at_minutes=30, breaking=False, cited=['a2'])])

    assert report.terms[0].cited == 0 and report.reached == 1
    assert 'reach: 1 flagged articles, 1 cited anywhere' in format_keyword_impact_report(
        report, width=110)


def test_configured_terms_that_never_fired_are_named_not_rendered_as_zero_rows():
    """`keyword_sweep` answers whether the corpus offered them a chance; this one names them."""
    report = _aggregate(_report(), [_flag('fomc statement')], [_pass(at_minutes=4, cited=['a1'])])
    text = format_keyword_impact_report(report, width=110)

    assert report.silent_terms == ['intervention', 'monetary policy assessment']
    assert '2 configured term(s) never fired' in text
    assert 'monetary policy assessment' in text


def test_an_envelope_is_reduced_to_the_fields_the_join_needs():
    """The envelope side of the join, read exactly as the outcome store serves it."""
    envelope = {
        'timestamp': '2026-09-14T12:09:00Z', 'trigger_reason': 'breaking',
        'result': [{'symbol': 'EURUSD', 'urgency': 0.9, 'is_breaking': True,
                    'sources': [{'article_id': 'a1'}, {'article_id': 'a2'}]},
                   {'symbol': 'GBPUSD', 'urgency': 0.5, 'is_breaking': False, 'sources': []}]}
    entry = _read_pass('forex_macro_sentiment', envelope, _NOW)

    assert entry.at == _NOW + timedelta(minutes=9)      # the envelope's own stamp, not the row's
    assert entry.breaking and entry.confirmed
    assert entry.cited == {'a1', 'a2'} and entry.urgency == 0.7


def test_an_envelope_without_a_usable_timestamp_falls_back_to_the_stored_row():
    """A row that cannot state its own time stays in the report instead of being dropped."""
    entry = _read_pass('forex_macro_sentiment', {'timestamp': 'not-a-date', 'result': []}, _NOW)

    assert entry.at == _NOW and not entry.breaking and entry.urgency is None


def test_the_unread_flags_are_named_with_their_age_at_flag():
    """Why a flag went unread is a question about freshness first, thresholds second.

    Retrieval only ever considers articles inside its recency window and the detector does not look
    at publication age at all — so an article already days old when it fired was spent before it was
    made, and no floor change could have rescued it. The two medians put that side by side.
    """
    report = _aggregate(_report(),
                        [_flag('fomc statement', article='fresh', age_minutes=6),
                         _flag('intervention', article='stale', at_minutes=30,
                               age_minutes=60 * 24 * 9, source='forexlive',
                               title='investingLive Asia-pacific FX news wrap')],
                        [_pass(at_minutes=8, cited=['fresh'])])
    text = format_keyword_impact_report(report, width=110)

    assert report.read_age_s == 6 * 60 and report.unread_age_s == 9 * 24 * 3600
    assert [(e.source_id, e.woke) for e in report.unread_examples] == [('forexlive', False)]
    assert 'age at flag (median, published → flagged): cited 6m · never read 216h00m' in text
    assert 'never read: forexlive · 216h00m old · investingLive' in text


def test_a_flag_that_woke_a_pass_and_went_unread_says_both():
    """The expensive case: it cost an LLM call and delivered no evidence — the line says so."""
    report = _aggregate(_report(), [_flag('intervention', article='a5')],
                        [_pass(at_minutes=2, cited=['other'])])

    assert report.unread_examples[0].woke
    assert 'woke a pass' in format_keyword_impact_report(report, width=110)


def test_an_article_without_a_publication_date_is_left_out_of_the_median():
    """A feed that carries no date gets no invented age — the sample names it, the median does not.

    The store's estimated publish date is not a fact this report may age against: it would turn a
    missing datum into a freshness verdict nobody measured.
    """
    report = _aggregate(_report(),
                        [_flag('intervention', article='dateless', age_minutes=None),
                         _flag('intervention', article='dated', at_minutes=5, age_minutes=120)],
                        [])

    assert report.unread_age_s == 120 * 60                      # only the dated one
    assert [e.age_at_flag_s for e in report.unread_examples] == [120 * 60, None]
    assert 'never read: fed_press · — old' in format_keyword_impact_report(report, width=110)


def test_the_unread_sample_is_capped_and_newest_first():
    """A window of hundreds must not print hundreds — the sample shows what happens NOW."""
    flags = [_flag('intervention', article=f'a{n}', at_minutes=n) for n in range(9)]
    report = _aggregate(_report(), flags, [])

    assert len(report.unread_examples) == 5
    assert report.flagged_articles == 9
