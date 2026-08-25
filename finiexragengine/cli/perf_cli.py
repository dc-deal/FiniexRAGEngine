"""CLI entry point: performance report from the billing log (ISSUE_32) — 'where did the time go'."""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.perf_report import format_perf_report
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(description='API latency report from the billing log')
    parser.add_argument('--since', default=None,
                        help='window: 7d, 30d, or all; omitted, reports.perf.window applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    resolved = resolve('perf', manager.get_config().reports, {'window': args.since})
    print(format_parameter_line(resolved.applied))
    print(format_perf_report(build_report('perf', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
