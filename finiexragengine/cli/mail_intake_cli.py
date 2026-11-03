"""CLI entry point: what the e-mail sources took in, refused and extracted, per day (#138)."""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.mail_intake_report import format_mail_intake_report
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Mail intake per day and source: seen, accepted, refused by reason, stories, cost')
    parser.add_argument('--since', default=None,
                        help='window: 7d, 30d, or all; omitted, the configured window applies')
    args = parser.parse_args()
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')
    manager = AppConfigManager()
    resolved = resolve('mail_intake', manager.get_config().reports, {'window': args.since})
    print(format_parameter_line(resolved.applied))
    print(format_mail_intake_report(build_report('mail_intake', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
