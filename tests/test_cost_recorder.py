"""Tests for cost derivation + recording (ISSUE_23).

`test_derive_*` is pure math. `test_record_*` writes to the canonical `cost_log` table in the
isolated, migration-built test schema (`clean_db`, ISSUE_14) and needs a reachable Postgres
(skipped otherwise) — no API budget is touched.
"""
import psycopg
import pytest

from finiexragengine.core.observability.cost_recorder import CostRecorder, derive_usd
from finiexragengine.types.config_types.app_config_types import ModelPrice, PricingConfig

_TABLE = 'cost_log'
_PRICING = PricingConfig(models={
    'text-embedding-3-small': ModelPrice(input_per_1k=0.00002),
    'gpt-4o-mini': ModelPrice(input_per_1k=0.00015, output_per_1k=0.0006),
})


def test_derive_usd_embedding_input_only():
    assert derive_usd(_PRICING, 'text-embedding-3-small', 10_000) == pytest.approx(0.0002)


def test_derive_usd_chat_input_plus_output():
    # 1000/1k*0.00015 + 500/1k*0.0006 = 0.00015 + 0.0003
    assert derive_usd(_PRICING, 'gpt-4o-mini', 1000, 500) == pytest.approx(0.00045)


def test_derive_usd_unknown_model_is_zero():
    assert derive_usd(_PRICING, 'mystery-model', 1000, 1000) == 0.0


@pytest.fixture
def recorder(clean_db: str) -> CostRecorder:
    return CostRecorder(clean_db, _PRICING)


def test_record_writes_row_and_returns_usd(recorder, clean_db):
    usd = recorder.record('ingest_news', 'text-embedding-3-small', 10_000, pipeline_id='p')
    assert usd == pytest.approx(0.0002)
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT section, model, total_tokens, usd_cost, pipeline_id FROM {_TABLE}')
        row = cur.fetchone()
    assert row[0] == 'ingest_news'
    assert row[1] == 'text-embedding-3-small'
    assert row[2] == 10_000
    assert row[3] == pytest.approx(0.0002)
    assert row[4] == 'p'


def test_record_persists_duration_ms(recorder, clean_db):
    # ISSUE_32: the API-call latency rides the same row as the tokens.
    recorder.record('llm_eval', 'gpt-4o-mini', 1000, 500, duration_ms=2718.0)
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT duration_ms FROM {_TABLE}')
        assert cur.fetchone()[0] == pytest.approx(2718.0)


def test_record_persists_model_snapshot(recorder, clean_db):
    # The served model (response.model) rides the row: alias retargets become visible;
    # the pricing lookup still keys on the configured name.
    usd = recorder.record('llm_eval', 'gpt-4o-mini', 1000, 500,
                          model_snapshot='gpt-4o-mini-2024-07-18')
    assert usd == pytest.approx(0.00045)                 # priced by the alias, not the snapshot
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT model, model_snapshot FROM {_TABLE}')
        assert cur.fetchone() == ('gpt-4o-mini', 'gpt-4o-mini-2024-07-18')


def test_alias_retarget_warns(recorder, caplog):
    # The dangerous moment: same alias, different served snapshot -> series shift, warn.
    recorder.record('llm_eval', 'gpt-4o-mini', 100, model_snapshot='gpt-4o-mini-2024-07-18')
    with caplog.at_level('WARNING'):
        recorder.record('llm_eval', 'gpt-4o-mini', 100,
                        model_snapshot='gpt-4o-mini-2025-03-01')
    assert any('retargeted' in r.message for r in caplog.records)


def test_stable_snapshot_stays_silent(recorder, caplog):
    recorder.record('llm_eval', 'gpt-4o-mini', 100, model_snapshot='gpt-4o-mini-2024-07-18')
    with caplog.at_level('WARNING'):
        recorder.record('llm_eval', 'gpt-4o-mini', 100,
                        model_snapshot='gpt-4o-mini-2024-07-18')
    assert not any('retargeted' in r.message for r in caplog.records)


def test_session_accumulators_track_this_process(recorder):
    # The RunFooter echo reads these — what *this* pass spent, no re-query needed.
    assert recorder.session_tokens == 0 and recorder.session_usd == 0.0
    recorder.record('ingest_news', 'text-embedding-3-small', 10_000)          # 0.0002
    recorder.record('llm_eval', 'gpt-4o-mini', 1000, 500, duration_ms=100.0)  # 0.00045
    assert recorder.session_tokens == 11_500
    assert recorder.session_usd == pytest.approx(0.00065)
