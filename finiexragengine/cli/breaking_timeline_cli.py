"""CLI entry point: the breaking state timeline (ISSUE_82) — the series behind the episode count.

Its own command, not a flag on the funnel report (ISSUE_104). The two answer different questions and
print different shapes: the funnel asks *how did detection perform*, this asks *should you believe
the episode count* — the per-pass on/off series with the verdict-flip count next to it. A flag that
decides which of two reports you get is a second program wearing the first one's name.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.breaking_timeline_report import (
    format_breaking_timeline_report,
)
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
        description='Breaking state timeline: the per-pass on/off series and its flip count')
    # No argparse defaults (ISSUE_104): omitted, `reports.breaking_timeline.window` applies.
    parser.add_argument('--since', default=None,
                        help='window: 7d, 30d, or all; omitted, the configured window applies')
    parser.add_argument('--symbol', default=None,
                        help='narrow to one symbol (e.g. XRPUSD); omitted, every symbol')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    resolved = resolve('breaking_timeline', manager.get_config().reports,
                       {'window': args.since, 'symbol': args.symbol})
    print(format_parameter_line(resolved.applied))
    print(format_breaking_timeline_report(
        build_report('breaking_timeline', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
