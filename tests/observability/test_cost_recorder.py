"""Tests for cost derivation + recording (ISSUE_23).

`test_derive_*` is pure math. `test_record_*` writes to the canonical `cost_log` table in the
isolated, migration-built test schema (`clean_db`, ISSUE_14) and needs a reachable Postgres
(skipped otherwise) — no API budget is touched.
"""
import asyncio

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


# --- ISSUE_74: per-pass attribution without serialization -----------------------------------


def test_pass_scope_collects_only_its_own_calls(recorder):
    recorder.record('ingest_news', 'text-embedding-3-small', 10_000)          # before the scope
    with recorder.pass_scope() as spend:
        recorder.record('llm_eval', 'gpt-4o-mini', 1000, 500)                 # 0.00045
    recorder.record('ingest_news', 'text-embedding-3-small', 10_000)          # after the scope
    assert spend.usd == pytest.approx(0.00045)
    assert spend.tokens == 1500
    # The session totals keep accumulating everything — `ingest_cli`'s footer depends on it.
    assert recorder.session_usd == pytest.approx(0.00085)


def test_record_outside_any_scope_still_works(recorder):
    # The CLI paths record without a scope; that must stay a plain no-op, not a crash.
    recorder.record('llm_eval', 'gpt-4o-mini', 1000, 500)
    assert recorder.session_usd == pytest.approx(0.00045)


def test_concurrent_passes_do_not_cross_attribute(recorder):
    """The guarantee the removed global lock used to provide (ISSUE_74).

    Two passes run at once in worker threads — the exact scenario serialization prevented. Each
    must see only its own spend. This is the single assertion that makes removing the lock safe,
    because `metadata.cost_usd` on every persisted envelope is derived from it.
    """
    async def _scenario():
        async def one_pass(prompt_tokens: int) -> float:
            with recorder.pass_scope() as spend:
                # Real threads, real overlap: the scope is opened on the loop and must survive
                # the context copy `asyncio.to_thread` makes.
                await asyncio.to_thread(recorder.record, 'llm_eval', 'gpt-4o-mini',
                                        prompt_tokens, 0)
                await asyncio.sleep(0.01)          # hold the scope open while the other records
                return spend.usd

        return await asyncio.gather(one_pass(1000), one_pass(4000))

    small, large = asyncio.run(_scenario())
    assert small == pytest.approx(0.00015)         # 1000/1k * 0.00015
    assert large == pytest.approx(0.0006)          # 4000/1k * 0.00015
    assert recorder.session_usd == pytest.approx(0.00075)   # the total still sees both


# --- ISSUE_87: why the pass ran, stamped on every row it produces ---------------------------


def test_the_pass_reason_lands_on_every_row_of_the_pass(recorder, clean_db):
    """One binding covers the whole pass — including calls no call site could annotate itself.

    An eval pass pays for its query embeddings as well as the completion; an ingest pass pays for
    embeddings and has no envelope at all. Reading the reason off the scope is what lets both
    answer "what do out-of-band wakes cost us" without threading a value through every caller.
    """
    with recorder.pass_scope('breaking'):
        recorder.record('ingest_query', 'text-embedding-3-small', 100)
        recorder.record('llm_eval', 'gpt-4o-mini', 1000, 500)
    recorder.record('ingest_news', 'text-embedding-3-small', 100)   # outside any scope

    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT section, trigger_reason FROM cost_log ORDER BY id')
        assert cur.fetchall() == [('ingest_query', 'breaking'),
                                  ('llm_eval', 'breaking'),
                                  # NULL, not a guessed default: nobody said why this one ran.
                                  ('ingest_news', None)]


def test_concurrent_passes_do_not_cross_attribute_their_reason(recorder, clean_db):
    """The ISSUE_74 isolation guarantee, extended to the new column.

    A breaking wake and a scheduled tick genuinely overlap in production — two eval workers on
    their own clocks. If the binding leaked between them, the cost rows would claim the wrong
    cause, which is worse than having no column: a wrong answer reads as a confident one.
    """
    async def _scenario() -> None:
        async def one_pass(reason: str, prompt_tokens: int) -> None:
            with recorder.pass_scope(reason):
                await asyncio.to_thread(recorder.record, 'llm_eval', 'gpt-4o-mini',
                                        prompt_tokens, 0)
                await asyncio.sleep(0.01)      # hold the scope open while the other records
                await asyncio.to_thread(recorder.record, 'llm_eval', 'gpt-4o-mini',
                                        prompt_tokens, 0)

        await asyncio.gather(one_pass('breaking', 1000), one_pass('scheduled', 4000))

    asyncio.run(_scenario())
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        cur.execute('SELECT trigger_reason, prompt_tokens FROM cost_log')
        rows = cur.fetchall()
    assert sorted(rows) == [('breaking', 1000), ('breaking', 1000),
                            ('scheduled', 4000), ('scheduled', 4000)]
