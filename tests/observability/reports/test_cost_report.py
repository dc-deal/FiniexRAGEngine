"""Tests for the cost report (ISSUE_23/12) — real spend + config-driven projection.

`test_format_*` is pure rendering (no DB). `test_build_*` / `test_prediction_*` seed a cost_log
(+ an outcomes table) and need a reachable pgvector Postgres — skipped otherwise; no API budget.
"""
import json

import pytest

pytest.importorskip('psycopg')
import psycopg  # noqa: E402

from finiexragengine.core.observability.cost_recorder import CostRecorder  # noqa: E402
from finiexragengine.core.observability.reports.cost_report import (  # noqa: E402
    CostReport,
    EvalPipelineInfo,
    LineItem,
    PipelineProjection,
    Prediction,
    RealWindow,
    build_cost_report,
    format_cost_report,
)
from finiexragengine.types.config_types.app_config_types import (  # noqa: E402
    ModelPrice,
    PricingConfig,
)

_TABLE = 'cost_log'
_OUTCOMES = 'outcomes'
_PRICING = PricingConfig(models={
    'text-embedding-3-small': ModelPrice(input_per_1k=0.00002),
    'gpt-4o-mini': ModelPrice(input_per_1k=0.00015, output_per_1k=0.0006),
})


# --- pure rendering -------------------------------------------------------------------

def _report(prediction):
    win = [RealWindow('this week', 5, 1000, 0.05,
                      [LineItem('crypto_sentiment', 3, 600, 0.03),
                       LineItem('crypto_news', 2, 400, 0.02)]),
           RealWindow('this month', 5, 1000, 0.05, []),
           RealWindow('all-time', 5, 1000, 0.05,
                      [LineItem('crypto_sentiment', 3, 600, 0.03),
                       LineItem('crypto_news', 2, 400, 0.02)])]
    return CostReport(real=win, prediction=prediction, spent_all_usd=0.05, credit_usd=10.0)


def test_format_separates_real_and_prediction_with_warning():
    prediction = Prediction(
        per_pipeline=[PipelineProjection('crypto_sentiment', 0.003, 144.0, 0.432,
                                         symbol_count=8, overridden=True)],
        usd_per_day=0.432, usd_per_week=3.024, usd_per_month=12.96, sampled_passes=20)
    text = format_cost_report(_report(prediction))
    assert 'REAL spend' in text and 'PREDICTION' in text
    assert 'crypto_sentiment' in text and 'crypto_news' in text     # per-pipeline attribution
    assert 'EXTRAPOLATED' in text and '⚠️' in text                  # projection clearly marked
    assert '/day' in text and '/week' in text and '/month' in text
    assert 'sym' in text and 'ovr' in text                          # symbol count + override cols
    assert 'yes' in text                                            # the override flag renders
    assert 'remaining' in text and '9.95' in text                   # 10.00 − 0.05


def test_format_without_any_passes_says_no_projection():
    text = format_cost_report(_report(None))
    assert 'no real passes yet to project' in text
    assert 'REAL spend' in text                                     # real part still renders


# --- DB-backed build ------------------------------------------------------------------

@pytest.fixture
def seeded(clean_db: str) -> str:
    recorder = CostRecorder(clean_db, _PRICING)
    # Real billing rows, attributed per pipeline / source-set.
    recorder.record('llm_eval', 'gpt-4o-mini', 1000, 500, pipeline_id='p1')      # 0.00045
    recorder.record('llm_eval', 'gpt-4o-mini', 1000, 500, pipeline_id='p1')      # 0.00045
    recorder.record('ingest_news', 'text-embedding-3-small', 10_000,
                    pipeline_id='news_set')                                      # 0.0002
    # Two persisted passes for p1 → avg cost/pass = 0.02 (drives the projection).
    with psycopg.connect(clean_db) as conn, conn.cursor() as cur:
        for cost in (0.01, 0.03):
            cur.execute(f'INSERT INTO {_OUTCOMES} (pipeline_id, ts, status, envelope) '
                        'VALUES (%s, now(), %s, %s)',
                        ('p1', 'success', json.dumps({'metadata': {'cost_usd': cost}})))
        conn.commit()
    return clean_db


def test_build_aggregates_real_spend_per_pipeline(seeded):
    report = build_cost_report(seeded, credit_usd=10.0)
    all_time = next(w for w in report.real if w.label == 'all-time')
    by = {item.label: item for item in all_time.by_pipeline}
    assert by['p1'].usd == pytest.approx(0.0009)          # two llm_eval calls
    assert by['news_set'].usd == pytest.approx(0.0002)
    assert report.spent_all_usd == pytest.approx(0.0011)
    assert report.remaining_usd == pytest.approx(10.0 - 0.0011)


def test_prediction_projects_real_cost_per_pass_over_config_cadence(seeded):
    report = build_cost_report(
        seeded, eval_pipelines={'p1': EvalPipelineInfo(600, symbol_count=2, overridden=True)})
    assert report.prediction is not None
    proj = {p.pipeline_id: p for p in report.prediction.per_pipeline}
    assert proj['p1'].usd_per_pass == pytest.approx(0.02)        # avg of 0.01 + 0.03
    assert proj['p1'].passes_per_day == pytest.approx(144.0)     # 86400 / 600
    assert proj['p1'].usd_per_day == pytest.approx(0.02 * 144)
    assert proj['p1'].symbol_count == 2 and proj['p1'].overridden is True
    assert report.prediction.usd_per_week == pytest.approx(0.02 * 144 * 7)
    assert report.prediction.usd_per_month == pytest.approx(0.02 * 144 * 30)


def test_no_eval_pipelines_means_no_prediction(seeded):
    report = build_cost_report(seeded, eval_pipelines={})
    assert report.prediction is None                            # nothing to project


def test_build_survives_missing_cost_table(db_dsn):
    # The table the report points at does not exist: 'nothing spent yet', not a crash.
    report = build_cost_report(db_dsn, cost_table='cost_log_never_created', credit_usd=5.0)
    assert all(w.usd == 0.0 for w in report.real) and report.spent_all_usd == 0.0
    assert report.remaining_usd == pytest.approx(5.0)


# --- the price basis: how old is the table every USD figure comes from? ------------------

def test_the_report_dates_the_price_basis_it_derived_from():
    # Every USD figure in this report is derived from a hand-maintained price table, and there is no
    # pricing API to check it against. A derived number whose basis carries no date cannot be
    # audited — so the age is rendered, always, at the top.
    from datetime import date, datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    fresh = _report(None)
    fresh.prices_checked = today
    assert fresh.prices_checked_days_ago == 0
    assert f'prices verified {today.isoformat()} (today)' in format_cost_report(fresh)

    old = _report(None)
    old.prices_checked = today - timedelta(days=73)
    assert old.prices_checked_days_ago == 73
    assert '(73 days ago)' in format_cost_report(old)

    # Singular reads correctly — the line is on every cost report anyone ever runs.
    yesterday = _report(None)
    yesterday.prices_checked = today - timedelta(days=1)
    assert '(1 day ago)' in format_cost_report(yesterday)


def test_a_missing_check_date_says_so_instead_of_reading_as_current():
    # `None` is a real state: nobody recorded when the table was held against the vendor. Rendering
    # nothing there would let an undated basis pass for a current one — the same mistake as reading
    # an unmeasured corpus as an empty one.
    report = _report(None)
    assert report.prices_checked is None
    assert report.prices_checked_days_ago is None

    text = format_cost_report(report)
    assert 'NO verification date recorded' in text
    assert 'prices verified' not in text


def test_the_age_carries_no_verdict():
    # Deliberately no STALE threshold: picking a staleness number here would be inventing a policy
    # nobody chose, and ISSUE_67's pricing probe is the mechanism meant to CHECK rather than remind.
    from datetime import datetime, timedelta, timezone

    ancient = _report(None)
    ancient.prices_checked = datetime.now(timezone.utc).date() - timedelta(days=900)
    text = format_cost_report(ancient)

    assert '(900 days ago)' in text
    for verdict in ('STALE', 'OUTDATED', '⚠️ prices', 'WARNING'):
        assert verdict not in text


def test_the_builder_actually_carries_the_date_onto_the_report(db_dsn):
    # The bug this pins: the parameter was accepted by `build_cost_report` and dropped on the floor —
    # the signature took it, both `return CostReport(...)` sites ignored it, and the report rendered
    # "NO verification date recorded" against a config that carried one. The tests passed because
    # they built `CostReport` directly and never went through the builder. Accepting an argument and
    # discarding it is invisible to every test that does not exercise the seam.
    from datetime import date

    report = build_cost_report(db_dsn, prices_checked=date(2026, 8, 28))
    assert report.prices_checked == date(2026, 8, 28)
    assert 'prices verified 2026-08-28' in format_cost_report(report)
