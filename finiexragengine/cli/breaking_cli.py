"""CLI entry point: breaking-detection report (ISSUE_11) — reaction time + the flagged→confirmed funnel.

`--timeline` switches to the second question (ISSUE_82): *should you believe the episode count* —
the per-pass on/off series behind it, with the verdict-flip count next to the episode count.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.breaking_report import (
    build_breaking_report,
    format_breaking_report,
)
from finiexragengine.core.observability.reports.breaking_timeline_report import (
    build_breaking_timeline_report,
    format_breaking_timeline_report,
)
from finiexragengine.core.pipeline.breaking_episode_rule import groupings_from_configs
from finiexragengine.utils.console_encoding import use_utf8_output
from finiexragengine.utils.report_window import parse_since


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Breaking-detection report: reaction time + flagged→confirmed funnel')
    parser.add_argument('--since', default='7d', help='window: 7d, 30d, or all')
    parser.add_argument('--timeline', nargs='?', const='', metavar='SYMBOL',
                        help='per-pass breaking state series instead of the funnel; optionally '
                             'for one symbol (e.g. --timeline XRPUSD)')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    since, label = parse_since(args.since)
    # Episode boundaries are per-pipeline config (ISSUE_82), so both surfaces are told the rules.
    # Registry via the manager factory — the only load path that applies the user_configs overlay.
    registry = AppConfigManager().build_pipeline_registry()
    rules = groupings_from_configs(p.get_config() for p in registry.list_pipelines())

    if args.timeline is not None:
        report = build_breaking_timeline_report(database_url, since, since_label=label,
                                                symbol=args.timeline, rules=rules)
        print(format_breaking_timeline_report(report))
        return

    print(format_breaking_report(
        build_breaking_report(database_url, since, since_label=label, rules=rules)))


if __name__ == '__main__':
    main()
