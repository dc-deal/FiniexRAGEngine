"""LiveDisplay — the rich dashboard renderer (ISSUE_26). Pure render(), no Live context."""
from datetime import datetime, timedelta, timezone

from rich.console import Console

from finiexragengine.core.pipeline.breaking_episode import EPISODE_GAP
from finiexragengine.core.ui.engine_stats import (
    EngineStats,
    IngestSnapshot,
    LlmSnapshot,
    RetrievalSnapshot,
    SourcesSnapshot,
)
from finiexragengine.core.ui.live_display import LiveDisplay

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
    # (the reused reasoning). Added at real `now` so they render as live (within EPISODE_GAP).
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
    # A last-seen older than EPISODE_GAP means the episode closed by the gap rule → 'N ago', not live.
    now = datetime.now(timezone.utc)
    stats = _stats()
    stats.add_breaking_episode('BTCUSD', 'SELL', 'old crash story', 'engine 2m',
                               at=now - EPISODE_GAP - timedelta(minutes=5))
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
