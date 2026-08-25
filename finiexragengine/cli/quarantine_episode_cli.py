"""CLI entry point: the poll-by-poll run-up to one quarantine episode (ISSUE_84).

The drill-down under the quarantine history, and its own command because it is its own report
(ISSUE_104): the history lists episodes, this reconstructs the minutes that produced one decision.

The timeline prefers the poll journal (full resolution) and falls back to the copy frozen into the
episode at decision time — the journal keeps 14 days while an episode series is read for months.
"""
import argparse
import os
from datetime import datetime, timezone

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.core.observability.reports.source_quarantine_report import (
    format_quarantine_episode,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='One quarantine episode with the poll-by-poll run-up that produced it')
    parser.add_argument('source_id', help='the feed the episode belongs to')
    parser.add_argument('started_at', metavar='UTC_TIMESTAMP',
                        help="the episode's start, ISO-8601 (e.g. 2026-08-15T05:04:04)")
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')
    try:
        started_at = datetime.fromisoformat(args.started_at)
    except ValueError:
        parser.error(f'expected an ISO timestamp, got {args.started_at!r}')
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    manager = AppConfigManager()
    resolved = resolve('source_quarantine_episode', manager.get_config().reports,
                       {'source_id': args.source_id, 'episode_start': started_at})
    print(format_parameter_line(resolved.applied))
    episode = build_report('source_quarantine_episode', database_url, manager, resolved.params)
    if episode is None:
        parser.error(f'no episode for {args.source_id} starting at {args.started_at}')
    print(format_quarantine_episode(episode, args.source_id))


if __name__ == '__main__':
    main()
