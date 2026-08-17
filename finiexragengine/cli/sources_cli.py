"""CLI entry point: everything the engine knows about its feeds.

Two reports, deliberately in one place because the operator's question spans both: the health rows
(ISSUE_11 — reliability, flags/quarantine, the problem log) and the poll journal (ISSUE_76 —
latency percentiles, the slow-vs-dead verdict, and the outages measured as gaps in the poll
series). Health says *whether* a feed is delivering; the journal says *how* it has been behaving.

`--history <source_id>` switches to the third question (ISSUE_84): *what did we do about it, and
was that proportionate* — one feed's quarantine episodes, the rung each reached, and the polls the
policy cost kept apart from the polls an outage cost.
"""
import argparse
import os
from datetime import datetime, timezone
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
from finiexragengine.core.observability.reports.source_quarantine_report import (
    build_quarantine_episode,
    build_source_quarantine_report,
    format_quarantine_episode,
    format_source_quarantine_report,
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
    parser.add_argument('--history', metavar='SOURCE_ID',
                        help='quarantine history for one feed instead of the fleet overview: '
                             'every episode, the rung it reached, and what it cost (ISSUE_84)')
    parser.add_argument('--episode', metavar='UTC_TIMESTAMP',
                        help='with --history: the poll-by-poll run-up to one episode, by its '
                             'start time (e.g. 2026-08-15T05:04:04)')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    if args.history:
        _print_history(parser, database_url, args)
        return

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


def _print_history(parser: argparse.ArgumentParser, database_url: str,
                   args: argparse.Namespace) -> None:
    """The per-feed quarantine history (ISSUE_84) — parameter reception only, no logic here."""
    manager = AppConfigManager()
    since, since_label = parse_since(args.since if args.since != '7d' else '30d')
    if args.episode:
        try:
            started_at = datetime.fromisoformat(args.episode)
        except ValueError:
            parser.error(f'--episode expects an ISO timestamp, got {args.episode!r}')
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        episode = build_quarantine_episode(database_url, args.history, started_at)
        if episode is None:
            parser.error(f'no episode for {args.history} starting at {args.episode}')
        print(format_quarantine_episode(episode, args.history))
        return
    report = build_source_quarantine_report(
        database_url, args.history, since, since_label=since_label,
        ladder_reset_hours=manager.get_config().source_health.ladder_reset_hours)
    print(format_source_quarantine_report(report))


if __name__ == '__main__':
    main()
