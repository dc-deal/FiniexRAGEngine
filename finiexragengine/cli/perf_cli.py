"""CLI entry point: performance report from the billing log (ISSUE_32) — 'where did the time go'."""
import argparse
import os
from datetime import datetime, timedelta, timezone
from typing import Tuple

from finiexragengine.core.observability.reports.perf_report import (
    build_perf_report,
    format_perf_report,
)


def _parse_since(value: str) -> Tuple[datetime, str]:
    """'7d' / '30d' / '14' -> (since_datetime, label); 'all' -> from the epoch."""
    if value == 'all':
        return datetime(1970, 1, 1, tzinfo=timezone.utc), 'all-time'
    days = int(value[:-1] if value.endswith('d') else value)
    return datetime.now(timezone.utc) - timedelta(days=days), f'{days}d'


def main() -> None:
    parser = argparse.ArgumentParser(description='API latency report from the billing log')
    parser.add_argument('--since', default='7d', help='window: 7d, 30d, or all')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    since, label = _parse_since(args.since)
    print(format_perf_report(build_perf_report(database_url, since, since_label=label)))


if __name__ == '__main__':
    main()
