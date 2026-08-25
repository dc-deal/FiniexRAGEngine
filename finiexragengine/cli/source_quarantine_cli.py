"""CLI entry point: one feed's quarantine history (ISSUE_84).

Its own command rather than a mode of the source overview (ISSUE_104). The overview answers *is the
fleet delivering*; this answers *what did we do about one feed, and was it proportionate* — every
episode, the rung it reached, and the polls the policy cost kept apart from the polls an outage
cost. Different question, different shape, own entry point.

The poll-by-poll run-up to a single episode is a third report again: `quarantine_episode_cli`.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.core.observability.reports.source_quarantine_report import (
    format_source_quarantine_report,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description="One feed's quarantine episodes: the rung each reached and what it cost")
    parser.add_argument('source_id', help='the feed to read (e.g. theblock)')
    parser.add_argument('--since', default=None,
                        help='window: 30d, 90d, or all; omitted, the configured window applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    resolved = resolve('source_quarantine', manager.get_config().reports,
                       {'window': args.since, 'source_id': args.source_id})
    print(format_parameter_line(resolved.applied))
    print(format_source_quarantine_report(
        build_report('source_quarantine', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
