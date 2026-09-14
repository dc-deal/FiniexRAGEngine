"""CLI entry point: config generations (ISSUE_116) — when was which configuration live?

`config_fingerprints` says what a fingerprint stood for; this says when it ran, per stream, with the
span it actually held and what it produced in that span. The question it exists for is
*"was this configuration alive throughout my window"* — which the registry's `first_seen`/`last_seen`
invite and cannot answer, because two edge points are not an interval.

Read-only over the activation log and the outcome store — no LLM, no embedding call, no write.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.generations_report import (
    format_generations_report,
)
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Config generations: which configuration was live on which stream, and when')
    parser.add_argument('pipeline_id', nargs='?', default='',
                        help='the stream to show; omitted, every stream the log recorded')
    parser.add_argument('--since', default=None,
                        help='window: 30d, 90d, or all; omitted, reports.generations.window applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # No registry check on the id, deliberately — unlike the per-source-set reports: this log keeps
    # the generations of streams that have since been removed from the configuration, and refusing
    # to name one would hide exactly the history the table is for.
    manager = AppConfigManager()
    resolved = resolve('generations', manager.get_config().reports,
                       {'window': args.since, 'pipeline_id': args.pipeline_id or None})
    print(format_parameter_line(resolved.applied))
    print(format_generations_report(build_report('generations', database_url, manager,
                                                 resolved.params)))


if __name__ == '__main__':
    main()
