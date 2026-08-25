"""CLI entry point: the source overview — feed reliability next to the poll journal.

Two reports in one view, deliberately and without a switch: health says *whether* a feed is
delivering (ISSUE_11 — reliability, flags/quarantine, the problem log) and the journal says *how* it
has been behaving (ISSUE_76 — latency percentiles, the slow-vs-dead verdict, outages as gaps in the
series). The operator's question spans both, so both print together.

What used to hide behind flags here now has its own command, because a parameter that decides
*which* report you get is a second program wearing this one's name (ISSUE_104):
`source_quarantine_cli`, `quarantine_episode_cli`, `source_contribution_cli`.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.source_contribution_report import (
    build_source_contribution_report,
    format_source_contribution_report,
)
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.core.observability.reports.source_health_report import (
    format_source_health_report,
)
from finiexragengine.core.observability.reports.source_latency_report import (
    format_source_latency_report,
)
from finiexragengine.core.observability.reports.source_quarantine_report import (
    format_quarantine_episode,
    format_source_quarantine_report,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Source report: feed reliability, flags/quarantine, problems, and the '
                    'poll journal (latency, slow-vs-dead verdict, outages)')
    # No argparse default (ISSUE_104): the configured window is the default, and a flag that always
    # carries a value would make `reports.*.window` unreadable — a config value nothing can reach.
    parser.add_argument('--since', default=None,
                        help='poll-journal window: 7d, 30d, or all (health rows are lifetime); '
                             'omitted, reports.source_latency.window applies')
    parser.add_argument('--recent-problems', type=int, default=None,
                        help='how many recent problems to list; omitted, '
                             'reports.source_health.recent_problems applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Both reports need inputs only the config can answer — the configured and disabled source ids,
    # and each feed's own fetch deadline. That resolution lives in the report catalog (ISSUE_104),
    # so the console and the API build the identical report from the identical inputs; this CLI is
    # back to what CLAUDE.md says it is, parameter reception.
    manager = AppConfigManager()
    reports_config = manager.get_config().reports

    # Config declares, the flag overrides, and the operator is told which of the two applied
    # (ISSUE_104) — the same resolution the API runs, through a different door.
    health = resolve('source_health', reports_config, {'recent_problems': args.recent_problems})
    print(format_parameter_line(health.applied))
    print(format_source_health_report(
        build_report('source_health', database_url, manager, health.params),
        recent_problems=health.params.options['recent_problems']))

    latency = resolve('source_latency', reports_config, {'window': args.since})
    print()
    print(format_parameter_line(latency.applied))
    print(format_source_latency_report(
        build_report('source_latency', database_url, manager, latency.params)))


if __name__ == '__main__':
    main()
