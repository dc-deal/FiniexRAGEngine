"""CLI entry point: keyword sweep (ISSUE_121) — what would this vocabulary flag?

Replays a candidate list, or the configured one, over the stored corpus: hits per term, how many of
them landed on a feed at or above `keyword_source_weight`, and a zero reported as a finding rather
than left to read as "the event has not happened yet". Read-only — no LLM, no embedding call, no
write — so it belongs on the report catalog under the rule #120 pinned, and the HTTP surface answers
the same question with the same resolution.

The terms file is one term per line; blank lines and `#` comments are ignored, so a candidate
vocabulary can carry its own notes while it is being argued about.
"""
import argparse
import os
from pathlib import Path
from typing import List

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.keyword_sweep_report import (
    format_keyword_sweep_report,
)
from finiexragengine.core.observability.reports.report_catalog import (
    build_report,
    format_parameter_line,
    resolve,
)
from finiexragengine.exceptions.ragengine_errors import ConfigurationError
from finiexragengine.utils.console_encoding import use_utf8_output


def _terms_from(path: str) -> List[str]:
    """One term per line; blanks and `#` comments dropped."""
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]


def main() -> None:
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Keyword sweep: what a vocabulary would flag, replayed from the stored corpus')
    parser.add_argument('source_set_id', nargs='?', default='',
                        help='the set to sweep; omitted, every configured set')
    parser.add_argument('--since', default=None,
                        help='window: 14d, 30d, or all; omitted, reports.keyword_sweep.window '
                             'applies')
    parser.add_argument('--terms-file', default=None,
                        help='candidate vocabulary, one term per line; omitted, the CONFIGURED '
                             'vocabulary is swept — which answers what the running list is doing')
    parser.add_argument('--term', action='append', dest='terms',
                        help='a single candidate term; repeatable, combines with --terms-file')
    parser.add_argument('--normalizer', default=None,
                        help="restrict the corpus to one text treatment: 'v1' for normalised rows, "
                             "'' for the raw pre-ISSUE_112 ones; omitted, whatever is there")
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    # An unknown id is a usage error, not an empty sweep: the catalog would filter it to nothing and
    # print a blank table, which reads as "this set matches nothing" rather than "no such set".
    if args.source_set_id:
        try:
            manager.build_source_set_registry().get(args.source_set_id)
        except ConfigurationError as exc:
            parser.error(str(exc))

    terms = list(args.terms or [])
    if args.terms_file:
        terms += _terms_from(args.terms_file)

    resolved = resolve('keyword_sweep', manager.get_config().reports,
                       {'window': args.since, 'normalizer': args.normalizer,
                        'terms': terms or None,
                        'source_set_id': args.source_set_id or None})
    print(format_parameter_line(resolved.applied))
    reports = build_report('keyword_sweep', database_url, manager, resolved.params)
    for index, report in enumerate(reports):
        if index:
            print()
        print(format_keyword_sweep_report(report))


if __name__ == '__main__':
    main()
