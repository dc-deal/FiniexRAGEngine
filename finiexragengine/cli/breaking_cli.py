"""CLI entry point: breaking-detection report (ISSUE_11) — reaction time + the flagged→confirmed funnel."""
import argparse
import os

from finiexragengine.core.observability.reports.breaking_report import (
    build_breaking_report,
    format_breaking_report,
)
from finiexragengine.utils.console_encoding import use_utf8_output
from finiexragengine.utils.report_window import parse_since


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Breaking-detection report: reaction time + flagged→confirmed funnel')
    parser.add_argument('--since', default='7d', help='window: 7d, 30d, or all')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    since, label = parse_since(args.since)
    report = build_breaking_report(database_url, since, since_label=label)
    print(format_breaking_report(report))


if __name__ == '__main__':
    main()
