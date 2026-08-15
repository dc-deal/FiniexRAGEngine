"""CLI entry point: performance report from the billing log (ISSUE_32) — 'where did the time go'."""
import argparse
import os

from finiexragengine.core.observability.reports.perf_report import (
    build_perf_report,
    format_perf_report,
)
from finiexragengine.utils.report_window import parse_since


def main() -> None:
    parser = argparse.ArgumentParser(description='API latency report from the billing log')
    parser.add_argument('--since', default='7d', help='window: 7d, 30d, or all')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    since, label = parse_since(args.since)
    print(format_perf_report(build_perf_report(database_url, since, since_label=label)))


if __name__ == '__main__':
    main()
