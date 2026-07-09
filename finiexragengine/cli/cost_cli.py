"""CLI entry point: cost report from the billing log (ISSUE_23) — the 'what did we spend' button."""
import argparse
import os
from datetime import datetime, timedelta, timezone

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.cost_report import (
    build_cost_report,
    format_cost_report,
)


def _parse_since(value: str):
    """'7d' / '30d' / '14' -> (since_datetime, label); 'all' -> from the epoch."""
    if value == 'all':
        return datetime(1970, 1, 1, tzinfo=timezone.utc), 'all-time'
    days = int(value[:-1] if value.endswith('d') else value)
    return datetime.now(timezone.utc) - timedelta(days=days), f'{days}d'


def main() -> None:
    parser = argparse.ArgumentParser(description='Cost report from the billing log')
    parser.add_argument('--since', default='7d', help='window: 7d, 30d, or all')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    since, label = _parse_since(args.since)
    cfg = AppConfigManager().get_config()
    report = build_cost_report(
        database_url, since, credit_usd=cfg.cost.account_credit_usd,
        budget_usd=cfg.cost.budget_usd, since_label=label)
    print(format_cost_report(report))


if __name__ == '__main__':
    main()
