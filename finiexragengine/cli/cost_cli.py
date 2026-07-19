"""CLI entry point: cost report (ISSUE_23) — real spend + a config-driven projection.

Real numbers come from the billing log; the projection extrapolates the real recent cost per
eval pass over the **effective** config's cadence (base + any user override) — clearly marked.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.cost_report import (
    EvalPipelineInfo,
    build_cost_report,
    format_cost_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Cost report: real spend (billing log) + a config-driven projection')
    parser.add_argument('--recent-passes', type=int, default=20,
                        help='how many recent real passes ground the per-pass average')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    cfg = manager.get_config()
    # Eval cadence from the EFFECTIVE config (base + user override) — the projection reflects
    # what actually runs, so a dev override (fewer symbols / other models) is included.
    registry = manager.build_pipeline_registry()
    eval_pipelines = {
        p.get_config().pipeline_id: EvalPipelineInfo(
            interval_seconds=p.get_config().trigger.interval_seconds,
            symbol_count=len(p.get_config().symbols),
            overridden=registry.is_overridden(p.get_config().pipeline_id))
        for p in registry.list_pipelines()}

    report = build_cost_report(database_url, eval_pipelines=eval_pipelines,
                               credit_usd=cfg.cost.account_credit_usd,
                               recent_passes=args.recent_passes)
    print(format_cost_report(report))


if __name__ == '__main__':
    main()
