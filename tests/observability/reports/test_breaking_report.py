"""Breaking report aggregation (ISSUE_11) — reaction math + episode grouping, DB-free.

Tests `_aggregate` directly with synthetic store rows (envelope dicts), so no DB is needed.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.breaking_report import (
    _aggregate,
    format_breaking_report,
)
from finiexragengine.core.pipeline.breaking_episode_rule import DEFAULT_EPISODE_GAP
from finiexragengine.core.pipeline.breaking_story_rule import StoryGrouping
from finiexragengine.types.ingest_types import DetectionReachability

_T0 = datetime(2026, 7, 13, 14, 0, 5, tzinfo=timezone.utc)
_T1 = datetime(2026, 7, 13, 14, 0, 12, tzinfo=timezone.utc)
_T3 = datetime(2026, 7, 13, 14, 0, 54, tzinfo=timezone.utc)

# Verbatim from the live journal (ISSUE_96 calibration). The two Pump-Token texts are one story the
# model restated; the routing-bug text is a different one on the same symbol.
_PUMP_A = ("Recent news highlights a significant price increase for Solana's Pump Token and a "
           'bullish chart pattern, indicating positive market sentiment.')
_PUMP_B = ("The recent article highlights a significant price increase for Solana's Pump Token "
           'and a bullish chart pattern, indicating positive sentiment and potential upside.')
_BUG = ('Recent articles highlight significant technical issues with Solana, including a routing '
        'bug that nearly caused a loss of finality and a near-freeze of the network.')


def _row(pipeline, t3, *, symbol='BTCUSD', is_breaking=True, published=None, fetched=None,
         signal='SELL', reason='', urgency=None, breaking_reason=None):
    source = {}
    if published is not None:
        source['published_at'] = published.isoformat()
    if fetched is not None:
        source['fetched_at'] = fetched.isoformat()
    return (pipeline, {
        'timestamp': t3.isoformat(),
        'result': [{'symbol': symbol, 'is_breaking': is_breaking, 'signal': signal,
                    'reasoning': reason, 'sources': [source] if source else [],
                    'breaking_reason': breaking_reason,
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
    # No arrow: flagged and confirmed are independent channels, not a yield (ISSUE_96).
    assert '3 flagged (corpus)' in out
    assert '1 confirmed episodes over 1 stories' in out
    assert 'not a yield' in out
    assert '→ 1 confirmed' not in out


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
    assert 'x' * 56 in out and 'x' * 57 not in out                  # cut to width budget (100−38−5=57 → 56 + …)


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


def test_the_report_counts_stories_next_to_episodes():
    """Two episodes of one story count once (ISSUE_96) — the number the episode count is read with.

    Both texts are the real 2026-08-17/18 SOLUSD Pump-Token reasons, which the calibration sweep
    confirmed are one story: the model restated the same headline twenty times over fourteen hours.
    """
    base = datetime(2026, 8, 17, 18, 40, tzinfo=timezone.utc)
    rows = [
        _row('p', base - timedelta(days=5), symbol='SOLUSD', signal='SELL', reason=_BUG),
        _row('p', base, symbol='SOLUSD', signal='BUY', reason=_PUMP_A),
        _row('p', base + timedelta(hours=12, minutes=30), symbol='SOLUSD', signal='BUY',
             reason=_PUMP_B),
    ]
    report = _aggregate(rows, flagged=0, since_label='7d')
    assert report.confirmed_episodes == 3
    assert report.total_stories == 2                    # the two Pump-Token episodes are one story
    assert report.rows[0].stories == 2
    pump_ids = {episode.story_id for episode in report.episodes if 'Pump Token' in episode.reason}
    assert len(pump_ids) == 1                           # and they carry the same id


def test_the_listing_brackets_episodes_that_share_a_story():
    """The grouping has to be readable, not merely counted — a re-derived number nobody can check
    is exactly what the story measure exists to replace."""
    base = datetime(2026, 8, 17, 18, 40, tzinfo=timezone.utc)
    rows = [
        _row('p', base - timedelta(days=5), symbol='SOLUSD', signal='SELL', reason=_BUG),
        _row('p', base, symbol='SOLUSD', signal='BUY', reason=_PUMP_A),
        _row('p', base + timedelta(hours=12, minutes=30), symbol='SOLUSD', signal='BUY',
             reason=_PUMP_B),
    ]
    out = format_breaking_report(_aggregate(rows, flagged=0, since_label='7d'), width=140)
    assert '┐' in out and '┘' in out
    assert 'story rule (read-time)' in out              # named, like the episode rule


def test_a_lone_episode_carries_no_bracket():
    rows = [_row('p', datetime(2026, 8, 17, 18, 40, tzinfo=timezone.utc), symbol='SOLUSD',
                 signal='BUY', reason='Recent news highlights the MoneyGram expansion onto Solana.')]
    out = format_breaking_report(_aggregate(rows, flagged=0, since_label='7d'), width=140)
    assert '┐' not in out and '┘' not in out and '├' not in out


def test_the_story_measure_clusters_on_reasoning_not_on_breaking_reason():
    """ISSUE_64 Phase 2 must not move ISSUE_96's substrate — the guard for that.

    `story_similarity = 0.45` was calibrated over 1,455 real `reasoning` texts; the shared
    boilerplate they carry is exactly what the IDF learns to suppress. `breaking_reason` is a
    purpose-built ≤25-word line with a different distribution, and it is empty on every envelope
    produced before prompt v3. Repointing the clustering at it would retire the calibration
    silently, so: `reason` measures, `breaking_reason` displays.

    Here the two Pump-Token episodes are one story by their `reasoning` while their
    `breaking_reason` lines share almost no vocabulary. They must still count as one.
    """
    base = datetime(2026, 8, 17, 18, 40, tzinfo=timezone.utc)
    rows = [
        _row('p', base, symbol='SOLUSD', signal='BUY', reason=_PUMP_A,
             breaking_reason='Pump Token doubles in an hour; Solana desks chase the move'),
        _row('p', base + timedelta(hours=12, minutes=30), symbol='SOLUSD', signal='BUY',
             reason=_PUMP_B,
             breaking_reason='Memecoin launchpad volumes hit a record; SOL bid follows through'),
    ]
    report = _aggregate(rows, flagged=0, since_label='7d')
    assert report.confirmed_episodes == 2
    assert report.total_stories == 1                    # grouped by `reasoning`, as calibrated
    # ...while the listing shows the purpose-built line.
    assert report.episodes[0].display_reason.startswith('Pump Token doubles')
    assert report.episodes[0].reason.startswith('Recent news highlights')


def test_the_listing_falls_back_to_reasoning_before_prompt_v3():
    """Every archived episode predates v3 and carries no breaking_reason — it still renders."""
    report = _aggregate([_row('p', _T3, symbol='SOLUSD', reason=_BUG)], flagged=0, since_label='7d')
    episode = report.episodes[0]
    assert episode.breaking_reason == ''
    assert episode.display_reason == _BUG


# --- the seam the tests above cannot see (ISSUE_106 build, 2026-08-25) --------------------

def test_the_builder_forwards_the_story_rule_it_was_given(monkeypatch):
    """`build_breaking_report(stories=...)` must reach `_aggregate`.

    It did not. Every test in this file drives `_aggregate` directly — which honours the rule — so
    the *pass-through above it* was never exercised, and the report silently grouped every pipeline
    by the schema default while `stories_applied` printed that default as the rule it had applied.
    Harmless the day it was found (both pipelines resolved to exactly the default, so no number
    moved), and wrong the moment anyone tunes `story_similarity` — silently, and with a false
    provenance line. Exactly the shape of the two-groupings divergence ISSUE_82 spent weeks on.
    """
    import finiexragengine.core.observability.reports.breaking_report as module

    seen = {}

    def fake_aggregate(rows, flagged, since_label, rules=None, stories=None, reachability=None):
        seen['stories'] = stories
        seen['rules'] = rules
        seen['reachability'] = reachability
        return module.BreakingReport(since_label, [], 0, 0)

    monkeypatch.setattr(module, '_aggregate', fake_aggregate)
    # No DB: the psycopg call is replaced too — this test is about the argument, not the query.
    monkeypatch.setattr(module.psycopg, 'connect',
                        lambda *a, **kw: (_ for _ in ()).throw(module.psycopg.Error('no db')))

    grouping = StoryGrouping(similarity=0.61, window=timedelta(days=1))
    try:
        module.build_breaking_report('postgresql://nowhere', _T0,
                                     stories={'crypto_sentiment': grouping})
    except Exception:
        pass   # the DB path is expected to fail; the assertion is on what was forwarded
    else:
        assert seen['stories'] == {'crypto_sentiment': grouping}


def test_the_report_shows_whether_the_thresholds_can_still_fire():
    # Part C of ISSUE_106: this is the report an operator opens when nothing is flagging, so
    # "the threshold is out of reach for the feeds that run" belongs here — not only in a boot line
    # nobody scrolls back to. The census renders either way: "nothing reported" must be
    # distinguishable from "nothing checked".
    healthy = DetectionReachability(source_set_id='forex_news', declared=18, active=11,
                                    mid_cluster_size=3, high_cluster_size=5,
                                    keyword_source_weight=0.9, max_active_weight=1.0,
                                    at_or_above_gate=9)
    starved = DetectionReachability(source_set_id='crypto_news', declared=6, active=4,
                                    disabled_ids=['theblock', 'cryptoslate'],
                                    mid_cluster_size=3, high_cluster_size=5,
                                    keyword_source_weight=0.9, max_active_weight=1.0,
                                    at_or_above_gate=3)
    report = _aggregate([], 0, '7d')
    report.reachability = [healthy, starved]
    text = format_breaking_report(report, width=100)

    assert 'detection reachability: 2 source-set(s) checked · 1 with a path out of reach' in text
    assert 'crypto_news · 4 active feeds (6 declared, 2 out: theblock, cryptoslate)' in text
    assert 'forex_news · cluster thresholds 3/5 satisfiable by 11 active feeds' in text
    # The sentence that makes the finding actionable rather than decorative.
    assert 'reads exactly like a quiet news week' in text

    # And with nothing wrong it still says it checked.
    clean = _aggregate([], 0, '7d')
    clean.reachability = [healthy]
    assert 'all thresholds satisfiable' in format_breaking_report(clean, width=100)


def test_the_report_splits_the_flagged_count_by_the_path_that_fired():
    # The total on its own cannot tune either threshold — it is the sum of two near-independent
    # channels (ISSUE_106). The split can.
    report = _aggregate([], 0, '7d', by_trigger={'cluster': 31, 'keyword': 15})
    text = format_breaking_report(report, width=100)
    assert 'flagged by path: 31 cluster · 15 keyword' in text


def test_rows_flagged_before_the_column_existed_are_their_own_bucket():
    # Folding them into either path would invent evidence: their decision depended on the corpus
    # state at that instant and is irreconstructable, which is why there is no backfill.
    report = _aggregate([], 0, '7d', by_trigger={'cluster': 4, 'unrecorded': 42})
    text = format_breaking_report(report, width=100)
    assert 'flagged by path: 4 cluster · 42 unrecorded' in text
    assert 'not attributable to either path, and never backfilled' in text


def test_the_report_renders_the_read_time_count_when_quarantine_is_known():
    # The distinction the issue asked for: the boot line reports `enabled` (a config fact), the
    # report reports `pollable` (config minus whoever the health policy has out right now). Two
    # honestly different numbers — and a verdict that does not name its population is unverifiable.
    from finiexragengine.core.pipeline.detection_preflight import with_quarantine

    reach = DetectionReachability(source_set_id='crypto_news', declared=21, active=7,
                                  active_ids=['cryptonews', 'cointelegraph', 'decrypt', 'coindesk',
                                              'beincrypto', 'cryptopolitan', 'theblock'],
                                  mid_cluster_size=3, high_cluster_size=5,
                                  keyword_source_weight=0.9, max_active_weight=1.0,
                                  at_or_above_gate=5)
    report = _aggregate([], 0, '7d')
    report.reachability = [with_quarantine(reach, {'theblock', 'coindesk', 'fxstreet'})]
    text = format_breaking_report(report, width=110)

    # fxstreet belongs to the other set and must not count against this one.
    assert '5 of 7 enabled feeds pollable' in text
    assert '2 quarantined right now: coindesk, theblock' in text
    assert 'fxstreet' not in text
    assert 'quarantine not included' not in text
