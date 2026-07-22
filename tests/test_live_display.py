"""LiveDisplay — the rich dashboard renderer (ISSUE_26). Pure render(), no Live context."""
from datetime import datetime, timezone

from rich.console import Console

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


def _render(display: LiveDisplay) -> str:
    # Render the pure renderable to text — no rich.Live, no terminal probing.
    console = Console(record=True, width=110)
    console.print(display.render())
    return console.export_text()


def test_render_smoke_on_empty_stats():
    """A fresh engine renders every stage row + a pre-registered idle row per worker, no crash."""
    text = _render(LiveDisplay(_stats(), worker_count=4))
    for row in ('SOURCES', 'INGEST', 'RETRIEVAL', 'LLM', 'BUDGET', 'BREAKING'):
        assert row in text
    # Pre-registered worker ids show as idle rows before their first pass — never missing.
    assert 'crypto_news' in text and 'forex_news' in text
    assert 'idle' in text
    assert '4 workers' in text


def test_two_workers_render_as_separate_rows():
    """The clobbering fix: both source-sets and both pipelines get their own row."""
    stats = _stats()
    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=5, total=5))
    stats.set_sources('forex_news', SourcesSnapshot(last=_NOW, ok=7, total=7))
    stats.set_llm('crypto_sentiment', LlmSnapshot(last=_NOW, tokens=6698, cost_usd=0.0011,
                                                  duration_ms=2800, signals=['SELL', 'SELL']))
    stats.set_llm('forex_macro_sentiment', LlmSnapshot(last=_NOW, tokens=4102, cost_usd=0.0007,
                                                       duration_ms=2400, signals=['HOLD', 'BUY']))
    text = _render(LiveDisplay(stats, worker_count=4))
    assert '5/5 ok' in text and '7/7 ok' in text              # both source-sets, no clobber
    assert 'SELL/SELL' in text and 'HOLD/BUY' in text         # both pipelines' signals
    assert 'crypto_sentiment' in text and 'forex_macro_sentiment' in text


def test_render_reflects_a_snapshot_update():
    stats = _stats()
    stats.set_ingest('crypto_news', IngestSnapshot(last=_NOW, fetched=128, new=119,
                                                   cost_usd=0.0012, duration_ms=1700))
    stats.set_retrieval('crypto_sentiment', RetrievalSnapshot(last=_NOW, retrieved=14, symbols=2))
    text = _render(LiveDisplay(stats, worker_count=4))
    assert '128 fetched' in text and '119 new' in text
    assert '14 retrieved' in text


def test_healthy_sources_collapse_but_a_deviation_is_named():
    stats = _stats()
    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=6, total=6))
    assert '6/6 ok' in _render(LiveDisplay(stats))            # exception density: no detail when healthy

    stats.set_sources('crypto_news', SourcesSnapshot(last=_NOW, ok=5, total=6,
                                                     deviations=['cryptoslate quarantined']))
    text = _render(LiveDisplay(stats))
    assert '5/6 ok' in text
    assert 'cryptoslate quarantined' in text                 # only the deviation spends words


def test_activity_stream_shows_recent_events():
    stats = _stats()
    for i in range(30):
        stats.push_event('INGEST', f'pass {i}')
    text = _render(LiveDisplay(stats))
    assert 'activity' in text
    assert 'pass 29' in text                                  # newest is shown
    assert 'pass 0' not in text                               # old events scrolled past the window
