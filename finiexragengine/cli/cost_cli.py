"""CLI entry point: cost report (ISSUE_23) — real spend + a config-driven projection.

Real numbers come from the billing log; the projection extrapolates the real recent cost per
eval pass over the **effective** config's cadence (base + any user override) — clearly marked.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.cost_report import format_cost_report
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Cost report: real spend (billing log) + a config-driven projection')
    # No argparse defaults (ISSUE_104): omitted, `reports.cost.*` applies.
    parser.add_argument('--recent-passes', type=int, default=None,
                        help='how many recent real passes ground the per-pass average; omitted, '
                             'reports.cost.recent_passes applies')
    parser.add_argument('--since', default=None,
                        help='narrow the comparison to ONE window (7d, 30d, all); omitted, the '
                             'configured set reports.cost.windows is compared')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    # Window set, credit and the eval cadence behind the projection are resolved by the catalog
    # (ISSUE_104), so this console and the API report the identical spend from identical inputs.
    resolved = resolve('cost', manager.get_config().reports,
                       {'window': args.since, 'recent_passes': args.recent_passes})
    print(format_parameter_line(resolved.applied))
    print(format_cost_report(build_report('cost', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
