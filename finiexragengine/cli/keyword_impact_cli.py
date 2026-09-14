"""CLI entry point: keyword impact (ISSUE_124) — what did each term actually do?

The retrospective half of `keyword_sweep`. The sweep says what a vocabulary *would* flag; this joins
the flags it made to the envelopes they woke: per term, how often it fired, whether the woken pass
cited the article, how long flag-to-envelope took, and how that envelope's urgency compares with the
scheduled passes around it. Read-only over the corpus and the outcome store — no LLM, no embedding
call, no write — so the HTTP surface answers the same question with the same resolution.

A term that fires perfectly and wakes nothing looks exactly like a term that fires perfectly and
saves nine minutes. That difference is the report.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.keyword_impact_report import (
    format_keyword_impact_report,
)
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.utils.console_encoding import use_utf8_output


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Keyword impact: what each shipped term did, from flag to envelope')
    parser.add_argument('source_set_id', nargs='?', default='',
                        help='the set to measure; omitted, every configured set')
    parser.add_argument('--since', default=None,
                        help='window: 30d, 90d, or all; omitted, reports.keyword_impact.window '
                             'applies')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    # An unknown id is a usage error, not an empty report — the same rule the sweep's CLI follows:
    # the catalog would filter it to nothing and print a blank table, which reads as "this set did
    # nothing" rather than "no such set".
    if args.source_set_id:
        try:
            manager.build_source_set_registry().get(args.source_set_id)
        except ConfigurationError as exc:
            parser.error(str(exc))

    resolved = resolve('keyword_impact', manager.get_config().reports,
                       {'window': args.since, 'source_set_id': args.source_set_id or None})
    print(format_parameter_line(resolved.applied))
    reports = build_report('keyword_impact', database_url, manager, resolved.params)
    for index, report in enumerate(reports):
        if index:
            print()
        print(format_keyword_impact_report(report))


if __name__ == '__main__':
    main()
