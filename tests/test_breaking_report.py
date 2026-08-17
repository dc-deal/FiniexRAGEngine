"""Breaking report aggregation (ISSUE_11) — reaction math + episode grouping, DB-free.

Tests `_aggregate` directly with synthetic store rows (envelope dicts), so no DB is needed.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.breaking_report import (
    _aggregate,
    format_breaking_report,
)
from finiexragengine.core.pipeline.breaking_episode_rule import DEFAULT_EPISODE_GAP

_T0 = datetime(2026, 7, 13, 14, 0, 5, tzinfo=timezone.utc)
_T1 = datetime(2026, 7, 13, 14, 0, 12, tzinfo=timezone.utc)
_T3 = datetime(2026, 7, 13, 14, 0, 54, tzinfo=timezone.utc)


def _row(pipeline, t3, *, symbol='BTCUSD', is_breaking=True, published=None, fetched=None,
         signal='SELL', reason='', urgency=None):
    source = {}
    if published is not None:
        source['published_at'] = published.isoformat()
    if fetched is not None:
        source['fetched_at'] = fetched.isoformat()
    return (pipeline, {
        'timestamp': t3.isoformat(),
        'result': [{'symbol': symbol, 'is_breaking': is_breaking, 'signal': signal,
                    'reasoning': reason, 'sources': [source] if source else [],
                    'urgency': 0.9 if urgency is None and is_breaking else (urgency or 0.0)}],
    })


def test_reaction_math_engine_vs_end_to_end():
    report = _aggregate([_row('crypto_sentiment', _T3, published=_T0, fetched=_T1)],
                        flagged=3, since_label='7d')
    assert report.confirmed_episodes == 1 and report.flagged_candidates == 3
    row = report.rows[0]
    assert row.engine_reaction_s == [42.0]      # t3 − freshest fetched_at (54 − 12)
    assert row.end_to_end_s == [49.0]           # t3 − freshest published_at (54 − 5)


def test_consecutive_breakings_are_one_episode():
    base = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row('p', base, fetched=base - timedelta(seconds=30)),
        _row('p', base + timedelta(minutes=5), fetched=base),   # same story, within the gap
    ]
    report = _aggregate(rows, 0, '7d')
    assert report.confirmed_episodes == 1                       # one episode, not two
    assert report.rows[0].engine_reaction_s == [30.0]          # sampled on the FIRST only


def test_re_break_after_the_gap_is_a_new_episode():
    base = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    apart = DEFAULT_EPISODE_GAP + timedelta(minutes=1)
    rows = [_row('p', base), _row('p', base + apart)]
    assert _aggregate(rows, 0, '7d').confirmed_episodes == 2


def test_non_breaking_rows_open_nothing():
    # They are OBSERVED since ISSUE_82 (a pass above the exit gate holds a story open), but a
    # pipeline that never broke still stays out of the funnel table.
    report = _aggregate([_row('p', _T3, is_breaking=False, urgency=0.2)],
                        flagged=5, since_label='7d')
    assert report.confirmed_episodes == 0 and report.rows == []


def test_missing_fetched_at_still_reports_end_to_end():
    # A pre-ISSUE_11 envelope has no fetched_at → engine-reaction unavailable, e2e still works.
    report = _aggregate([_row('p', _T3, published=_T0)], 0, '7d')
    row = report.rows[0]
    assert row.engine_reaction_s == [] and row.end_to_end_s == [49.0]


def test_format_renders_windows_and_funnel():
    report = _aggregate([_row('crypto_sentiment', _T3, published=_T0, fetched=_T1)], 3, '7d')
    out = format_breaking_report(report)
    assert 'Breaking Detection' in out
    assert 'window: last 7d' in out
    assert '3 flagged → 1 confirmed' in out


def test_episode_listing_shows_started_duration_and_reason():
    # ISSUE_64: the per-episode listing groups by pipeline, one line each — started, duration, why.
    base = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row('crypto_sentiment', base, symbol='ETHUSD', signal='SELL',
             reason='greed rising — Musk confirms ETH buy-in', fetched=base - timedelta(seconds=30)),
        _row('crypto_sentiment', base + timedelta(minutes=5), symbol='ETHUSD', signal='SELL',
             reason='still hot', fetched=base),                     # same episode → extends duration
    ]
    report = _aggregate(rows, 0, '7d')
    assert len(report.episodes) == 1                                # one edge-triggered episode
    episode = report.episodes[0]
    assert episode.symbol == 'ETHUSD' and episode.signal == 'SELL'
    assert episode.duration_s == 300.0                              # 5-min span (last − start)
    assert episode.reason == 'greed rising — Musk confirms ETH buy-in'   # frozen at the start
    out = format_breaking_report(report, width=120)               # explicit width — no ambient TTY dep
    assert 'Breaking episodes — last 7d' in out
    assert 'ETHUSD' in out and 'Musk confirms ETH buy-in' in out
    assert '5.0m' in out                                            # duration rendered


def test_fanned_same_base_symbols_collapse_to_one_episode():
    # ISSUE_70 Schicht 2: ETHUSD + ETHEUR (base ETH) in one envelope → one asset-level episode in
    # the store report too (mirrors the live tracker), so the confirmed count is not doubled.
    t3 = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    env = ('crypto_sentiment', {'timestamp': t3.isoformat(), 'result': [
        {'symbol': 'ETHUSD', 'is_breaking': True, 'signal': 'SELL', 'reasoning': 'hack',
         'base_currency': 'ETH', 'sources': []},
        {'symbol': 'ETHEUR', 'is_breaking': True, 'signal': 'SELL', 'reasoning': 'hack',
         'base_currency': 'ETH', 'sources': []}]})
    report = _aggregate([env], 0, '7d')
    assert report.confirmed_episodes == 1                          # collapsed to one ETH episode
    assert len(report.episodes) == 1 and report.episodes[0].symbol == 'ETHUSD'


def test_episode_listing_groups_by_symbol_and_adapts_width():
    # ISSUE_64 feedback: cluster a pipeline's episodes by symbol (so signal consistency is scannable)
    # and cap the reason to the console width instead of a fixed cut.
    base = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    rows = [
        _row('p', base, symbol='ETHUSD', signal='BUY', reason='x' * 200),
        _row('p', base + timedelta(hours=2), symbol='ADAUSD', signal='SELL', reason='y'),
        _row('p', base + timedelta(hours=4), symbol='ETHUSD', signal='SELL', reason='z'),
    ]
    report = _aggregate(rows, 0, '7d')
    assert [e.symbol for e in report.episodes] == ['ADAUSD', 'ETHUSD', 'ETHUSD']   # grouped by symbol
    out = format_breaking_report(report, width=100)
    assert 'x' * 57 in out and 'x' * 58 not in out                  # cut to width budget (100−37−5=58 → 57 + …)


# --- ISSUE_81: the store path anchors like the live path ------------------------------------


def _multi_source_row(pipeline, t3, sources):
    """A row with several retrieved sources — the realistic shape (`(published, fetched)` pairs)."""
    return (pipeline, {
        'timestamp': t3.isoformat(),
        'result': [{'symbol': 'BTCUSD', 'is_breaking': True, 'signal': 'SELL', 'reasoning': '',
                    'sources': [{'published_at': p.isoformat(), 'fetched_at': f.isoformat()}
                                for p, f in sources]}],
    })


def test_reaction_ignores_stale_context_and_follows_the_freshest_source():
    """The store half of the ISSUE_81 fix — and the reason it matters retroactively.

    This report recomputes from persisted envelopes, so anchoring on the oldest source made every
    historical weekly report show the retrieval window (~21h in production) instead of a reaction.
    Fixing the anchor corrects the whole archive, not just runs after the fix.
    """
    t3 = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    report = _aggregate([_multi_source_row('p', t3, [
        (t3 - timedelta(hours=20), t3 - timedelta(hours=20)),   # stale context article
        (t3 - timedelta(seconds=45), t3 - timedelta(seconds=30)),   # the triggering one
    ])], flagged=1, since_label='7d')

    row = report.rows[0]
    assert row.engine_reaction_s == [30.0]
    assert row.end_to_end_s == [45.0]


def test_live_and_store_agree_on_the_same_envelope():
    """Live and store must produce the same number for the same data — the ISSUE_64 lesson.

    They are two independent implementations over one envelope; when they drifted before (the
    episode gap), the two surfaces quietly disagreed for weeks.
    """
    from finiexragengine.core.pipeline.breaking_episode import BreakingEpisodeTracker
    from finiexragengine.types.outcome_types import (
        ArticleRef, RunMetadata, SentimentEnvelope, SentimentResult)

    t3 = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    pairs = [(t3 - timedelta(hours=9), t3 - timedelta(hours=9)),
             (t3 - timedelta(minutes=4), t3 - timedelta(minutes=2)),
             (t3 - timedelta(hours=1), t3 - timedelta(minutes=55))]

    store_row = _aggregate([_multi_source_row('p', t3, pairs)], flagged=1, since_label='7d').rows[0]

    live = BreakingEpisodeTracker().observe(SentimentEnvelope(
        pipeline_id='p', outcome_type='sentiment_fear_greed', prompt_version='2', timestamp=t3,
        status='success', metadata=RunMetadata(model='m'),
        result=[SentimentResult(
            symbol='BTCUSD', signal='SELL', sentiment_score=-0.5, confidence=0.8, reasoning='',
            urgency=0.9, is_breaking=True,
            sources=[ArticleRef(article_id='a', url='u', title='t', published_at=p, fetched_at=f)
                     for p, f in pairs])])).started[0]

    assert store_row.engine_reaction_s[0] == live.engine_s == 120.0
    assert store_row.end_to_end_s[0] == live.end_to_end_s == 240.0


def test_live_and_store_agree_across_a_restart():
    """The parity claim has to survive a process restart, which it did not before ISSUE_82.

    `BreakingEpisodeTracker` used to start empty, so the boot pass re-opened an ongoing story as a
    fresh episode while the store report — re-deriving from the same rows — counted one. Measured
    on the live server: two of one week's 66 episodes were exactly this, opened 3 and 11 minutes
    after the previous breaking pass. Seeding the tracker from the store closes the gap, and this
    test drives both paths over one fixture to keep them closed.
    """
    from finiexragengine.core.pipeline.breaking_episode import BreakingEpisodeTracker
    from finiexragengine.types.outcome_types import (
        RunMetadata, SentimentEnvelope, SentimentResult)

    base = datetime(2026, 7, 13, 14, 0, 0, tzinfo=timezone.utc)
    stamps = [base + timedelta(minutes=10 * i) for i in range(4)]      # one 30-minute story

    def _envelope(ts):
        return SentimentEnvelope(
            pipeline_id='p', outcome_type='sentiment_fear_greed', prompt_version='2', timestamp=ts,
            status='success', metadata=RunMetadata(model='m'),
            result=[SentimentResult(symbol='BTCUSD', signal='SELL', sentiment_score=-0.5,
                                    confidence=0.8, reasoning='', urgency=0.9, is_breaking=True)])

    store_episodes = _aggregate([_row('p', ts) for ts in stamps], 0, '7d').confirmed_episodes

    # The engine restarts after the third pass; the replacement tracker is seeded from the store
    # exactly as `pipeline_assembler.build_episode_tracker` seeds it.
    before = BreakingEpisodeTracker()
    for ts in stamps[:3]:
        before.observe(_envelope(ts))
    after = BreakingEpisodeTracker()
    for ts in stamps[:3]:                                             # the seeding replay
        after.observe(_envelope(ts))
    boot = after.observe(_envelope(stamps[3]))

    assert store_episodes == 1
    assert boot.started == [], 'a seeded tracker must resume the story, not re-open it'
