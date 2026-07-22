"""CLI entry point: weekly report (ISSUE_27) — the console twin of the Telegram weekly.

Renders the same typed `WeeklyReport` the scheduler sends: cost + performance + source
health + retrieval coverage + breaking + storage + worker status. `--send` additionally
pushes the Telegram rendering to the configured chat (same model, second surface).
"""
import argparse
import asyncio
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.alerts.telegram_client import TelegramClient
from finiexragengine.core.alerts.telegram_weekly_format import render_weekly_messages
from finiexragengine.exceptions.alert_errors import TelegramError
from finiexragengine.core.observability.reports.weekly_report import (
    collect_weekly_report,
    format_weekly_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Weekly report: cost + performance + sources + coverage + status')
    parser.add_argument('--send', action='store_true',
                        help='also send the Telegram rendering to the configured chat')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    report = collect_weekly_report(manager, database_url)
    print(format_weekly_report(report))

    if args.send:
        telegram = manager.get_config().telegram
        if not (telegram.enabled and telegram.bot_token and telegram.chat_id):
            parser.error('telegram is not configured — enable it and set bot_token/chat_id '
                         'in the gitignored user_configs/app_config.json')
        messages = render_weekly_messages(report)
        try:
            asyncio.run(TelegramClient(telegram).send_messages(messages))
        except TelegramError as exc:
            # The report already printed in full above — delivery is a separate, best-effort
            # step. Fail on one line (no stack trace) with a non-zero exit, not a crash.
            raise SystemExit(
                f'⚠ not sent to Telegram — {exc}\n'
                '  (the report above is complete; --send delivery is what failed)')
        print(f'sent to Telegram ({len(messages)} message(s))')


if __name__ == '__main__':
    main()
