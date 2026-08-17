"""CLI entry point: everything the engine knows about its feeds.

Two reports, deliberately in one place because the operator's question spans both: the health rows
(ISSUE_11 — reliability, flags/quarantine, the problem log) and the poll journal (ISSUE_76 —
latency percentiles, the slow-vs-dead verdict, and the outages measured as gaps in the poll
series). Health says *whether* a feed is delivering; the journal says *how* it has been behaving.
"""
import argparse
import os
from typing import Dict

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.source_health_report import (
    build_source_health_report,
    format_source_health_report,
)
from finiexragengine.core.observability.reports.source_latency_report import (
    build_source_latency_report,
    format_source_latency_report,
)
from finiexragengine.utils.console_encoding import use_utf8_output
from finiexragengine.utils.report_window import parse_since


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Source report: feed reliability, flags/quarantine, problems, and the '
                    'poll journal (latency, slow-vs-dead verdict, outages)')
    parser.add_argument('--since', default='7d',
                        help='poll-journal window: 7d, 30d, or all (health rows are lifetime)')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # Currently-configured source ids across every set — anything in the store but not here is
    # an orphan (a removed feed) the report flags as safe-to-delete.
    manager = AppConfigManager()
    registry = manager.build_source_set_registry()
    configured_ids = {source.source_id
                      for source_set in registry.list_sets()
                      for source in source_set.sources}
    # Which of them are switched off on this machine — the store cannot know (health is what a
    # poll did; `enabled` is what the config says), so the report is told, and marks them rather
    # than presenting a disabled feed's frozen last poll as a current verdict.
    disabled_ids = {source.source_id
                    for source_set in registry.list_sets()
                    for source in source_set.sources
                    if not source.enabled}

    report = build_source_health_report(database_url, configured_ids,
                                        disabled_ids=disabled_ids)
    print(format_source_health_report(report))

    # The deadline each feed is actually judged against (ISSUE_76): its own override if it has one,
    # otherwise its set's default. The journal cannot know this — it records what happened, not what
    # was allowed — so without the map a p99 has nothing to be measured against.
    timeouts: Dict[str, int] = {
        source.source_id: source.timeout_seconds or source_set.fetch_timeout_seconds
        for source_set in registry.list_sets()
        for source in source_set.sources}

    since, since_label = parse_since(args.since)
    latency = build_source_latency_report(
        database_url, since, since_label=since_label, timeouts=timeouts,
        warn_ratio=manager.get_config().diagnostics.timeout_warn_ratio)
    print()
    print(format_source_latency_report(latency))


if __name__ == '__main__':
    main()
