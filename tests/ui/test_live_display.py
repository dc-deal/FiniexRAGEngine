"""LiveDisplay — the rich dashboard renderer (ISSUE_26). Pure render(), no Live context."""
from datetime import datetime, timedelta, timezone

from rich.console import Console

from finiexragengine.core.pipeline.breaking_episode_rule import DEFAULT_EPISODE_GAP
from finiexragengine.core.ui.engine_stats import (
    EngineStats,
    IngestSnapshot,
    LlmSnapshot,
    RetrievalSnapshot,
    SourcesSnapshot,
)
from finiexragengine.core.pipeline.ingest_worker import _flag_line
from finiexragengine.core.ui.live_display import LiveDisplay
from finiexragengine.types.ingest_types import FlaggedCandidate

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _stats() -> EngineStats:
    return EngineStats(source_set_ids=['crypto_news', 'forex_news'],
                       pipeline_ids=['crypto_sentiment', 'forex_macro_sentiment'])


def _render(stats: EngineStats, **kwargs) -> str:
    # Render to text via a fixed-size console — the SAME console the display measures with, so the
    # measured state-panel height matches what is printed (ISSUE_70 adaptive wrapping).
    console = Console(record=True, width=110, height=40)
    console.print(LiveDisplay(stats, console=console, **kwargs).render())
    return console.export_text()


def test_render_smoke_on_empty_stats():
    """A fresh engine renders every stage row + a pre-registered idle row per worker, no crash."""
    text = _render(_stats(), worker_count=4)
    for row in ('SOURCES', 'INGEST', 'RETRIEVAL', 'LLM', 'BUDGET', 'BREAKING'):
        assert row in text
    # Pre-registered worker ids show as idle rows before their first pass — never missing.
    assert 'crypto_news' in text and 'forex_news' in text
    assert 'idle' in text
    assert '4 workers' in text
    assert 'episodes' in text and 'none active' in text      # BREAKING section, empty until an episode


def test_breaking_section_lists_live_episodes_with_reason():
    # ISSUE_64: each confirmed episode is one line — symbol+signal, a live marker, and *why* it broke
    # (the reused reasoning). Added at real `now` so they render as live (within the episode gap).
    now = datetime.now(timezone.utc)
    stats = _stats()
    stats.add_breaking_episode('ADAUSD', 'SELL', 'regulatory probe cluster', 'engine 1.4m', at=now)
    stats.add_breaking_episode('ETHUSD', 'BUY', 'Musk confirms ETH buy-in', 'engine 12s', at=now)
    text = _render(stats, worker_count=4)
    assert 'ADAUSD SELL' in text and 'ETHUSD BUY' in text     # per-episode symbol+signal
    assert 'regulatory probe cluster' in text                 # the why (reused reasoning)
    assert 'Musk confirms ETH buy-in' in text
    assert '●' in text                                        # both just broke → live marker


def test_breaking_section_marks_an_ended_episode():
    # A last-seen older than the record's own gap means the episode closed → 'N ago', not live.
    # The gap rides on the record because it is per-pipeline config (ISSUE_82).
    now = datetime.now(timezone.utc)
    stats = _stats()
    stats.add_breaking_episode('BTCUSD', 'SELL', 'old crash story', 'engine 2m',
                               at=now - DEFAULT_EPISODE_GAP - timedelta(minutes=5))
    text = _render(stats, worker_count=4)
    assert 'BTCUSD SELL' in text
    assert 'ago' in text                                      # ended → recency, not a live dot


def test_two_workers_render_as_separate_rows():
    """The clobbering fix: both source-sets and both pipelines get their own row."""
    stats = _stats()
    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=5, total=5))
    stats.set_sources('forex_news', SourcesSnapshot(last=_NOW, ok=7, total=7))
    stats.set_llm('crypto_sentiment', LlmSnapshot(
        last=_NOW, tokens=6698, cost_usd=0.0011, duration_ms=2800,
        signals=[('BTCUSD', 'SELL', 'BTC', 'Bitcoin BTC'), ('ETHUSD', 'SELL', 'ETH', 'Ethereum ETH')]))
    stats.set_llm('forex_macro_sentiment', LlmSnapshot(
        last=_NOW, tokens=4102, cost_usd=0.0007, duration_ms=2400,
        signals=[('EURUSD', 'HOLD', 'EUR', 'Euro'), ('GBPUSD', 'BUY', 'GBP', 'Pound')]))
    text = _render(stats, worker_count=4)
    assert '5/5 ok' in text and '7/7 ok' in text              # both source-sets, no clobber
    assert 'BTCUSD:SELL' in text and 'ETHUSD:SELL' in text    # distinct queries → not merged
    assert 'EURUSD:HOLD' in text and 'GBPUSD:BUY' in text     # both pipelines' symbols
    assert 'crypto_sentiment' in text and 'forex_macro_sentiment' in text


def test_fanned_same_query_symbols_merge_into_one_chip():
    # ISSUE_70: ETHUSD + ETHEUR (same query "Ethereum ETH") render as ONE chip, call count shows.
    stats = _stats()
    stats.set_llm('crypto_sentiment', LlmSnapshot(
        last=_NOW, tokens=6698, cost_usd=0.0011, duration_ms=2800, calls=2,
        signals=[('BTCUSD', 'SELL', 'BTC', 'Bitcoin BTC'),
                 ('ETHUSD', 'HOLD', 'ETH', 'Ethereum ETH'), ('ETHEUR', 'HOLD', 'ETH', 'Ethereum ETH')]))
    text = _render(stats, worker_count=4)
    assert 'ETH·USD/EUR:HOLD' in text                         # fanned pair merged into one chip
    assert 'BTCUSD:SELL' in text                              # lone symbol unchanged
    assert '3 sym / 2 calls' in text                          # grouping visible in the count


def test_same_base_different_query_symbols_are_not_merged():
    # ISSUE_70 regression: USDJPY + USDCAD share base USD and (here) signal, but are DIFFERENT
    # analyses (distinct queries) — they must NOT merge into a false `USD·JPY/CAD` chip.
    stats = _stats()
    stats.set_llm('forex_macro_sentiment', LlmSnapshot(
        last=_NOW, tokens=4102, cost_usd=0.0007, duration_ms=2400,
        signals=[('USDJPY', 'SELL', 'USD', 'US Dollar Japanese Yen'),
                 ('USDCAD', 'SELL', 'USD', 'US Dollar Canadian Dollar')]))
    text = _render(stats, worker_count=4)
    assert 'USDJPY:SELL' in text and 'USDCAD:SELL' in text    # kept separate — distinct queries
    assert 'USD·JPY/CAD' not in text                          # the false-merge must not happen


def test_render_reflects_a_snapshot_update():
    stats = _stats()
    stats.set_ingest('crypto_news', IngestSnapshot(last=_NOW, fetched=128, new=119,
                                                   cost_usd=0.0012, duration_ms=1700))
    stats.set_retrieval('crypto_sentiment', RetrievalSnapshot(last=_NOW, retrieved=14, symbols=2))
    text = _render(stats, worker_count=4)
    assert '128 fetched' in text and '119 new' in text
    assert '14 retrieved' in text


def test_healthy_sources_collapse_but_a_deviation_is_named():
    stats = _stats()
    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=6, total=6))
    assert '6/6 ok' in _render(stats)            # exception density: no detail when healthy

    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=5, total=6,
                                                     deviations=['cryptoslate quarantined']))
    text = _render(stats)
    assert '5/6 ok' in text
    assert 'cryptoslate quarantined' in text                 # only the deviation spends words


def test_a_connectivity_event_replaces_the_per_feed_list(monkeypatch):
    # ISSUE_84: when the whole set is held by a local connectivity failure, naming seven blameless
    # feeds is exactly the noise the guard exists to remove — and it points the operator at the
    # feeds instead of at the host. The row says the one thing that is true.
    stats = _stats()
    stats.set_sources('forex_news', SourcesSnapshot(
        last=_NOW, ok=0, total=7,
        deviations=['ecb_press failed', 'fed_press failed', 'boe_news failed'],
        host_backoff_until=_NOW + timedelta(minutes=5),
        host_detail='forex_news 7/7 + crypto_news 5/5'))
    text = _render(stats)
    # The row is long enough to wrap inside the panel (ISSUE_70 measures the height for exactly
    # that), so the fleet breakdown is checked across the fold rather than on one physical line.
    unwrapped = ' '.join(text.replace('│', ' ').split())

    assert 'host connectivity' in text
    assert 'forex_news 7/7 + crypto_news 5/5' in unwrapped
    assert 'no quarantine' in text
    assert 'ecb_press failed' not in text                    # the feeds are not the story


def test_a_stalled_worker_paints_its_last_cell_red():
    # ISSUE_75: the cell that read a neutral `last 212h…` for nine days. A stalled worker must be
    # visually distinct from a healthy one — colour is the signal (the column has no room for a
    # glyph), so the assertion reads the rendered style, not the text.
    from finiexragengine.core.observability.stall_watchdog import StallWatchdog
    from finiexragengine.types.config_types.app_config_types import StallWatchdogConfig
    from finiexragengine.types.worker_types import WorkerState

    dead = WorkerState(name='ingest:crypto_news', kind='ingest', interval_seconds=15)
    dead.last_run_at = datetime.now(timezone.utc) - timedelta(days=9)
    watchdog = StallWatchdog(StallWatchdogConfig(), lambda: [dead])
    watchdog.check()                                          # opens the stall episode

    stats = _stats()
    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=5, total=5))
    stats.set_sources('forex_news', SourcesSnapshot(last=_NOW, ok=7, total=7))

    console = Console(record=True, width=110, height=40)
    console.print(LiveDisplay(stats, stall_watchdog=watchdog,
                              worker_count=4, console=console).render())
    # styles=True keeps the ANSI codes, so the assertion is on what the operator's eye actually
    # gets — `\x1b[1;31m` is rich's rendering of `red bold`.
    lines = console.export_text(styles=True).splitlines()
    stalled_line = next(line for line in lines if 'crypto_news' in line)
    healthy_line = next(line for line in lines if 'forex_news' in line)
    assert '\x1b[1;31m' in stalled_line, 'the stalled worker row must render red'
    assert '\x1b[1;31m' not in healthy_line, 'the healthy worker row must stay neutral'


def test_a_display_without_a_watchdog_renders_normally():
    # The CLI/test path passes no watchdog — no stall rendering, and above all no crash.
    stats = _stats()
    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=5, total=5))
    assert '5/5 ok' in _render(stats, worker_count=4)


def test_no_watchdog_renders_the_last_cell_as_unknown_rather_than_healthy():
    """ISSUE_126: the three states are stalled, checked-and-fine, and **nobody checked**.

    In one process an absent watchdog means this deployment has none, and a neutral cell is right.
    Fetched from another machine the same absence means the engine did not tell us, and neutral is
    the cell asserting health on no evidence — on the exact colour channel that exists because a
    neutral `last 212h…` went unnoticed for nine days. So it renders dim: legible, visibly not a
    verdict.
    """
    from finiexragengine.core.observability.stall_watchdog import StallWatchdog
    from finiexragengine.types.config_types.app_config_types import StallWatchdogConfig

    stats = _stats()
    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=5, total=5))

    def line(**kwargs) -> str:
        console = Console(record=True, width=110, height=40)
        console.print(LiveDisplay(stats, console=console, worker_count=4, **kwargs).render())
        return next(l for l in console.export_text(styles=True).splitlines()
                    if 'crypto_news' in l and 'last' in l)

    # A watchdog that ran and found nothing: a verdict, rendered plainly.
    checked = line(stall_watchdog=StallWatchdog(StallWatchdogConfig(), lambda: []))
    # No watchdog at all: not a verdict, and it must not look like one.
    unchecked = line()

    assert '\x1b[2m' in unchecked, 'an unchecked cell must be dim, not neutral'
    assert unchecked != checked, 'checked-and-fine must be distinguishable from nobody-checked'


def test_the_header_does_not_invent_a_zero_spend():
    """ISSUE_126: `$0.000 today` in the position of a measurement is a plausible wrong number.

    A quiet day and an engine that reported no budget at all look identical once a zero is on
    screen — and on a viewer the second one means "we were not told".
    """
    assert '— today' in _render(_stats(), worker_count=4)
    assert '$0.000 today' not in _render(_stats(), worker_count=4)


def test_activity_stream_shows_recent_events():
    stats = _stats()
    for i in range(30):
        stats.push_event('INGEST', f'pass {i}')
    text = _render(stats)
    assert 'activity' in text
    assert 'pass 29' in text                                  # newest is shown
    assert 'pass 0' not in text                               # old events scrolled past the window


def test_the_header_names_the_running_version():
    """A live console that does not say which build it shows makes "did the deploy land?" a guess.

    This session had to answer exactly that from commit timestamps and a report footer's wording.
    """
    display = LiveDisplay(EngineStats(), worker_count=4, version='0.3.2')
    assert 'FiniexRAGEngine v0.3.2 — up ' in display._header(datetime.now(timezone.utc))


def test_the_version_segment_is_omitted_when_unknown():
    """CLI and test paths build a display without config — no empty `v` in the header."""
    display = LiveDisplay(EngineStats(), worker_count=1)
    header = display._header(datetime.now(timezone.utc))
    assert header.startswith('FiniexRAGEngine — up ') and ' v' not in header


def test_an_episode_clipped_by_the_replay_window_renders_a_lower_bound():
    """ISSUE_82: the boot replay covers a bounded window, so a story that opened earlier has its
    start clipped to the edge. The row must not present that clipped span as the real duration."""
    now = datetime.now(timezone.utc)
    stats = _stats()
    stats.restore_breaking_episode('USDCAD', 'SELL', 'tariffs',
                                   started=now - timedelta(hours=4, minutes=47),
                                   last_seen=now - timedelta(minutes=3), gap_seconds=9000.0,
                                   started_bounded=True)
    text = _render(stats, worker_count=4)
    assert 'USDCAD SELL' in text
    assert '≥' in text                                   # a bound, not a measurement
    assert 'last 3m' in text                             # the freshness fact IS restored


def test_an_episode_whose_start_was_observed_shows_no_bound():
    # With a window wide enough to contain the opening, the duration is a measurement — the `≥`
    # would understate what the process actually knows.
    now = datetime.now(timezone.utc)
    stats = _stats()
    stats.restore_breaking_episode('BTCUSD', 'BUY', 'institutional interest',
                                   started=now - timedelta(hours=17, minutes=11),
                                   last_seen=now - timedelta(minutes=2), gap_seconds=9000.0)
    line = next(line for line in _render(stats, worker_count=4).splitlines()
                if 'BTCUSD BUY' in line)
    assert '17h11m' in line and '≥' not in line


def test_header_warnings_appear_only_while_their_condition_holds():
    """The convention for instance-wide conditions on the live console (ISSUE_9/ISSUE_26).

    `--live` suppresses the console log handler, so a condition an operator must notice cannot be a
    log line — it becomes a header segment that appears while it holds and vanishes when it stops.
    Adding the next one means one entry in `LiveDisplay._header_warnings`, never another
    `header +=` and never a row of its own: a row costs layout in every frame, a segment costs
    nothing once the condition clears.
    """
    from finiexragengine.core.ui.live_display import WARNING_MARK, LiveDisplay
    from finiexragengine.core.ui.engine_stats import EngineStats

    named = LiveDisplay(EngineStats(), worker_count=1, version='9.9.9', journal_named=True)
    assert named._header_warnings() == []
    assert WARNING_MARK not in named._header(datetime.now(timezone.utc))

    unnamed = LiveDisplay(EngineStats(), worker_count=1, version='9.9.9', journal_named=False)
    assert [w for w in unnamed._header_warnings() if 'journal' in w]
    header = unnamed._header(datetime.now(timezone.utc))
    assert WARNING_MARK in header and 'journal unnamed' in header
    # Appended to the running header, never replacing it — the operator still sees version/uptime.
    assert header.startswith('FiniexRAGEngine v9.9.9')


def test_the_breaking_row_names_the_path_that_flagged():
    """Which path did the flagging is the part that tells a vocabulary problem from a cluster one.

    A count alone ('3 detected') sent the operator to the log for the one fact the engine already
    had. Rendered only when a path is recorded, so a flag from before the split stays a plain
    number rather than claiming an unknown path.
    """
    stats = _stats()
    stats.add_breaking_detected(3, at=datetime.now(timezone.utc),
                                by_trigger={'keyword': 2, 'cluster': 1})
    assert '3 detected (1 cluster · 2 keyword)' in _render(stats)

    unsplit = _stats()
    unsplit.add_breaking_detected(1, at=datetime.now(timezone.utc))
    assert '1 detected · 0 confirmed' in _render(unsplit)


def test_a_flagged_pass_names_the_term_and_the_feed_in_the_activity_stream():
    """The activity line an ingest pass writes when it flags (ISSUE_26) — one line, not one per
    flag, so a noisy vocabulary cannot crowd the panel."""
    stats = _stats()
    stats.push_event('BREAKING', _flag_line('forex_news', [
        FlaggedCandidate(source_id='fed_press', title='Federal Reserve issues FOMC statement',
                         trigger='keyword', terms=('fomc statement',)),
        FlaggedCandidate(source_id='forexlive', title='FX news wrap', trigger='keyword',
                         terms=('intervention',))]))
    text = _render(stats)

    assert 'BREAKING' in text and 'keywords fomc statement' in text
    # The parts an operator acts on survive the panel's crop; the headline is what may be cut.
    assert 'fed_press (+1 more)' in text


def test_a_viewer_measures_the_producers_clock_not_its_own():
    """ISSUE_126: three numbers on this panel were the reader's clock in disguise.

    The header's uptime was `now - <the display object's construction>`, which in a viewer measures
    the viewer. Every `last <age>` cell was an engine timestamp minus the reader's clock, so the
    difference between two machines sat inside each one, invisible. And the SOURCES back-off
    countdown read the clock a second time inside the same frame.

    Given the producer's start and the instant it stamped its reading with, all three follow the
    producer — and a viewer whose own clock is minutes off prints the same panel either way.
    """
    engine_started = datetime(2026, 9, 22, 11, 30, tzinfo=timezone.utc)
    snapshot_at = datetime(2026, 9, 22, 13, 30, tzinfo=timezone.utc)

    stats = _stats()
    stats.set_sources('crypto_news',
                      SourcesSnapshot(last=snapshot_at - timedelta(minutes=5), ok=5, total=5))

    panel = _render(stats, worker_count=4,
                    started_at=engine_started, now_provider=lambda: snapshot_at)

    assert 'up 2h' in panel, 'uptime must be measured from the ENGINE start'
    assert 'last 5m' in panel, 'ages must be measured against the instant the engine stamped'


def test_an_unestablished_journal_identity_is_not_an_unnamed_one():
    """ISSUE_126: two different facts, and they send an operator to two different places.

    `False` says somebody must add a name to `journal_names`. `None` says this reading could not
    establish the identity at all — telling an operator to go and name a journal would be sending
    them to fix the wrong thing. In one process it is never None; a viewer is where it arrives.
    """
    unnamed = _render(_stats(), worker_count=4, journal_named=False)
    unknown = _render(_stats(), worker_count=4, journal_named=None)

    assert 'journal unnamed' in unnamed
    assert 'journal identity not established' in unknown
    assert 'journal unnamed' not in unknown
    assert 'journal' not in _render(_stats(), worker_count=4, journal_named=True)
