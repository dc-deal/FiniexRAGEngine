"""CLI entry point: retrieval drift (ISSUE_55 groundwork) — did the evidence move, not the answers.

The console half of `GET /v1/reports/retrieval_drift`. Both go through the report catalog, so the
two surfaces cannot drift apart in what they resolve or what they render.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.core.observability.reports.retrieval_drift_report import (
    format_retrieval_drift_report,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Retrieval drift: the funnel per pipeline, config fingerprint and weekday — '
                    'whether the evidence reaching the prompt moved when the setup changed')
    parser.add_argument('--since', default=None,
                        help='window: 14d, 30d, or all; omitted, reports.retrieval_drift.window '
                             'applies. Shorter than two weeks cannot hold two of the same weekday, '
                             'which is what the comparison needs')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    resolved = resolve('retrieval_drift', manager.get_config().reports, {'window': args.since})
    print(format_parameter_line(resolved.applied))
    print(format_retrieval_drift_report(
        build_report('retrieval_drift', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
