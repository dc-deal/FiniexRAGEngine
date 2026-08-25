"""CLI entry point: breaking-detection report (ISSUE_11) — reaction time + the flagged→confirmed funnel.

The per-pass on/off series behind the episode count is a different report and has its own command,
`breaking_timeline_cli` (ISSUE_104): a flag that decides which of two reports you get is a second
program wearing this one's name.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.breaking_report import format_breaking_report
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
        description='Breaking-detection report: reaction time + flagged→confirmed funnel')
    # No argparse default (ISSUE_104): the configured window applies when the flag is absent.
    parser.add_argument('--since', default=None,
                        help='window: 7d, 30d, or all; omitted, reports.<report>.window applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Episode boundaries are per-pipeline config (ISSUE_82) and the story measure has its own
    # thresholds (ISSUE_96). Resolving both from the registry is the catalog's job (ISSUE_104), so
    # the console and the API group identically by construction rather than by two matching edits.
    manager = AppConfigManager()
    reports_config = manager.get_config().reports

    funnel = resolve('breaking', reports_config, {'window': args.since})
    print(format_parameter_line(funnel.applied))
    print(format_breaking_report(build_report('breaking', database_url, manager, funnel.params)))


if __name__ == '__main__':
    main()
