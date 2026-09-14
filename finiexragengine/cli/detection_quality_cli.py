"""CLI entry point: detection quality (ISSUE_106) — what the detector flagged, and on what evidence.

The console half of `GET /v1/reports/detection_quality`. Both go through the report catalog, so the
two surfaces cannot drift apart in what they resolve or what they render.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.detection_quality_report import (
    format_detection_quality_report,
)
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Detection quality: flags per path, the neighbourhood each cluster flag was '
                    'made on, and the duplication ratio that separates corroboration from one '
                    'feed repeating itself')
    parser.add_argument('--since', default=None,
                        help='window: 7d, 30d, or all; omitted, '
                             'reports.detection_quality.window applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    resolve_config = manager.get_config().reports
    resolved = resolve('detection_quality', resolve_config, {'window': args.since})
    print(format_parameter_line(resolved.applied))
    print(format_detection_quality_report(
        build_report('detection_quality', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
