"""CLI entry point: breaking-detection report (ISSUE_11) — reaction time + the flagged→confirmed funnel."""
import argparse
import os
from datetime import datetime, timedelta, timezone
from typing import Tuple

from finiexragengine.core.observability.reports.breaking_report import (
    build_breaking_report,
    format_breaking_report,
)


def _parse_since(value: str) -> Tuple[datetime, str]:
    """'7d' / '30d' / '14' -> (since_datetime, label); 'all' -> from the epoch."""
    if value == 'all':
        return datetime(1970, 1, 1, tzinfo=timezone.utc), 'all-time'
    days = int(value[:-1] if value.endswith('d') else value)
    return datetime.now(timezone.utc) - timedelta(days=days), f'{days}d'


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Breaking-detection report: reaction time + flagged→confirmed funnel')
    parser.add_argument('--since', default='7d', help='window: 7d, 30d, or all')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    since, label = _parse_since(args.since)
    report = build_breaking_report(database_url, since, since_label=label)
    print(format_breaking_report(report))


if __name__ == '__main__':
    main()
