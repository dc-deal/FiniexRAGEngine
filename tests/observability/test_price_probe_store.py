"""Integration tests for PriceProbeStore — the probe's own history (ISSUE_67).

Skipped when psycopg or a reachable PostgreSQL is missing. Runs against the canonical
`price_probes` table in the isolated, migration-built test schema (`clean_db`, ISSUE_14), so
migration 016 is under test rather than hand-written DDL.

The point of the table is not the single reading but the record that says, months from now, whether
this probe is reliable enough to ever be trusted beyond shadow mode. So what is asserted is that a
failed probe leaves a row saying so — an absence would look exactly like a week nobody ran it.
"""
import psycopg
import pytest

from finiexragengine.core.observability.price_probe_store import PriceProbeStore
from finiexragengine.types.config_types.app_config_types import ModelPrice, PricingConfig
from finiexragengine.types.pricing_types import ProbedPrice

_TABLE = 'price_probes'
_PRICING = PricingConfig(models={
    'gpt-4o-mini': ModelPrice(input_per_1k=0.00015, output_per_1k=0.0006),
    'gpt-5-nano': ModelPrice(input_per_1k=0.00005, output_per_1k=0.0004)})


@pytest.fixture
def store(clean_db: str) -> PriceProbeStore:
    return PriceProbeStore(clean_db)


def _rows(database_url: str):
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT model, status, probed_input_per_1k, table_input_per_1k, '
                    f'delta_input_pct, probe_model FROM {_TABLE} ORDER BY model')
        return cur.fetchall()


def test_a_run_records_one_row_per_priced_model_with_its_delta(store, clean_db):
    written = store.record(
        _PRICING,
        [ProbedPrice(model='gpt-4o-mini', input_per_1k=0.00015, output_per_1k=0.0006),
         ProbedPrice(model='gpt-5-nano', input_per_1k=0.00004, output_per_1k=0.0004)],
        source_url='https://x.test/pricing', probe_model='gpt-4o-mini')

    assert written == 2
    rows = _rows(clean_db)
    assert [(row[0], row[1]) for row in rows] == [('gpt-4o-mini', 'ok'), ('gpt-5-nano', 'ok')]
    assert rows[0][4] == pytest.approx(0.0)              # unchanged leaf → zero drift, not NULL
    assert rows[1][4] == pytest.approx(-20.0)            # 0.00005 → 0.00004
    assert rows[1][5] == 'gpt-4o-mini'                   # which model READ the page


def test_an_unreadable_page_still_leaves_the_record_of_the_attempt(store, clean_db):
    """A run that wrote nothing and a week nobody ran would otherwise look identical."""
    written = store.record(_PRICING, [], source_url='https://x.test/pricing',
                           probe_model='gpt-4o-mini', readable=False)

    assert written == 2
    rows = _rows(clean_db)
    assert {row[1] for row in rows} == {'unreadable'}
    assert all(row[2] is None and row[4] is None for row in rows)   # no price is claimed
    assert all(row[3] for row in rows)                              # the table's value is kept


def test_a_model_absent_from_the_page_is_recorded_as_absent(store, clean_db):
    store.record(_PRICING,
                 [ProbedPrice(model='gpt-4o-mini', input_per_1k=0.00015, output_per_1k=0.0006),
                  ProbedPrice(model='gpt-5-nano', status='absent')],
                 source_url='https://x.test/pricing', probe_model='gpt-4o-mini')

    assert [(row[0], row[1]) for row in _rows(clean_db)] == [('gpt-4o-mini', 'ok'),
                                                             ('gpt-5-nano', 'absent')]


def test_a_failed_write_is_swallowed_so_a_weekly_job_survives_its_bookkeeping(clean_db):
    """The notice has already gone out by then — losing the row costs an explanation, not a signal."""
    broken = PriceProbeStore(clean_db, table='price_probes_missing')

    assert broken.record(_PRICING, [], source_url='https://x.test/pricing',
                         probe_model='gpt-4o-mini') == 0
