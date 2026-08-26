"""CLI entry point: prompt drift (ISSUE_110) — the score distribution per prompt version.

Its own command, like every other report (ISSUE_104). The question is specific and so is the shape:
*did the last prompt bump move the distribution, and in which direction per pipeline* — which is
exactly what a pooled number cannot answer, and what nothing in this engine could answer at all
until this report existed.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.prompt_drift_report import (
    format_prompt_drift_report,
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
        description='Prompt drift: the urgency distribution per prompt version, per pipeline')
    # No argparse defaults (ISSUE_104): omitted, `reports.prompt_drift.window` applies — 30d, wide
    # enough that the default window contains more than one version to compare.
    parser.add_argument('--since', default=None,
                        help='window: 7d, 30d, or all; omitted, the configured window applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    resolved = resolve('prompt_drift', manager.get_config().reports, {'window': args.since})
    print(format_parameter_line(resolved.applied))
    print(format_prompt_drift_report(
        build_report('prompt_drift', database_url, manager, resolved.params)))


if __name__ == '__main__':
    main()
