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
from finiexragengine.core.outcome.outcome_exporter import auto_export_weekly


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Weekly report: cost + performance + sources + coverage + status')
    parser.add_argument('--send', action='store_true',
                        help='also send the Telegram rendering to the configured chat')
    parser.add_argument('--no-export', action='store_true',
                        help='skip the closed-day JSONL archive export (on by default)')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    weekly_cfg = manager.get_config().weekly_report
    report = collect_weekly_report(manager, database_url)
    print(format_weekly_report(report))

    # Dump the closed-day archive alongside the report (default on; --no-export or the config
    # knob turns it off). Whole closed buckets only — idempotent, byte-identical to export_cli.
    if not args.no_export:
        result = auto_export_weekly(weekly_cfg, database_url)
        if result is not None:
            print(f'exported {len(result.files)} file(s), {result.total_lines} line(s) '
                  f'→ {weekly_cfg.export_dir}')

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
