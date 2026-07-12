"""Tests for the RunFooter (ISSUE_32) — the shared metrics block, pure rendering."""
from datetime import datetime, timezone

from finiexragengine.core.observability.run_footer import RunFooter
from finiexragengine.types.outcome_types import StageTiming

_TS = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _timing(stage: str, ms: float) -> StageTiming:
    return StageTiming(stage=stage, started_at=_TS, ended_at=_TS, duration_ms=ms)


def test_renders_divider_timings_and_cost():
    footer = RunFooter(timings=[_timing('retrieve', 65.0), _timing('llm', 2632.0)],
                       tokens_label='prompt 1975 · completion 58 · total 2033',
                       usd=0.000331, section='llm_eval')
    text = footer.render()
    assert text.startswith('--- run metrics ---')          # the ---- separated section
    assert 'retrieve 65ms · llm 2632ms · total 2697ms' in text
    assert 'cost $0.000331 (llm_eval)' in text


def test_aggregate_sums_repeated_stages_in_order():
    # One fetch/embed per source -> the footer shows one summed entry per stage.
    footer = RunFooter(timings=[_timing('fetch', 100.0), _timing('embed', 50.0),
                                _timing('fetch', 200.0), _timing('embed', 25.0)],
                       tokens_label='1,234 embedding', usd=0.0001,
                       section='ingest_news', aggregate=True)
    text = footer.render()
    assert 'fetch 300ms · embed 75ms · total 375ms' in text


def test_no_usd_means_no_cost_suffix():
    footer = RunFooter(timings=[_timing('fetch', 10.0)], tokens_label='0 embedding')
    assert 'cost' not in footer.render()


def test_empty_timings_render_placeholder():
    footer = RunFooter(timings=[], tokens_label='0 embedding', usd=0.0, section='ingest_news')
    assert '(no stages ran)' in footer.render()


def test_model_line_renders_when_set():
    # A print that costs tokens names what produced them (alias + served snapshot).
    footer = RunFooter(timings=[_timing('llm', 100.0)], tokens_label='t', usd=0.1,
                       section='llm_eval',
                       model_label='gpt-4o-mini (served gpt-4o-mini-2024-07-18)')
    text = footer.render()
    assert 'model       gpt-4o-mini (served gpt-4o-mini-2024-07-18)' in text
    # And stays absent when not set (ingest passes without an LLM, older callers).
    assert 'model' not in RunFooter(timings=[], tokens_label='t').render()
