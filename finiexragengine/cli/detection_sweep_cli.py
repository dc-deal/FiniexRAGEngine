"""CLI entry point: detection sweep (ISSUE_106) — what would each detector have flagged?

Read-only replay over the stored corpus: no LLM, no embedding calls, no writes. Answers the question
the live cluster path cannot — whether a different measure or a looser similarity would detect
corroboration, or merely admit one feed's daily template first.

**On the report catalog, and it took a correction to get there.** This file used to say the sweep was
excluded for being heavy — a self-join over embeddings. Weight is not the criterion the catalog
applies: `coverage` and `floor_profile` are absent because a cache miss inside them is a paid
embedding call, and a GET must never convert into spend. This report reads. It was excluded for a
property it does not have, while being needed three times in one session from a console on the
production host. Its size is bounded where the window ceiling already is — on the exposed surface,
by `sample`.
"""
import argparse
import os

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.detection_sweep_report import (
    DEFAULT_SIMILARITIES,
    format_detection_sweep_report,
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
        description='Detection sweep: what each candidate detector would have flagged, replayed '
                    'from the corpus')
    parser.add_argument('source_set_id', nargs='?', default='',
                        help='the set to sweep; omitted, every configured set')
    parser.add_argument('--since', default=None,
                        help='window: 7d, 30d, or all; omitted, reports.detection_sweep.window '
                             'applies')
    parser.add_argument('--sample', type=int, default=None,
                        help='how many recent articles to score as seeds; omitted, '
                             'reports.detection_sweep.sample applies')
    parser.add_argument('--similarity', type=float, action='append', dest='similarities',
                        help='override the grid; repeatable (default: '
                             + ', '.join(f'{s:.2f}' for s in DEFAULT_SIMILARITIES) + ')')
    # ISSUE_112 stamps the text treatment that produced the stored text. Two articles sharing a
    # feed's HTML boilerplate are similar because of the markup, so a grid read off a mixed sample
    # measures the wrong thing — this is how you compare like with like.
    parser.add_argument('--normalizer', default=None,
                        help="restrict the sample to one text treatment: 'v1' for normalised rows, "
                             "'' for the raw pre-ISSUE_112 ones; omitted, whatever is there (the "
                             'report then says the mix out loud)')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    manager = AppConfigManager()
    # An unknown id is a usage error, not an empty sweep: the catalog would filter it to nothing and
    # print a blank grid, which reads as "this set has no clusters" rather than "no such set".
    if args.source_set_id:
        try:
            manager.build_source_set_registry().get(args.source_set_id)
        except ConfigurationError as exc:
            parser.error(str(exc))

    resolved = resolve('detection_sweep', manager.get_config().reports,
                       {'window': args.since, 'sample': args.sample,
                        'similarities': args.similarities, 'normalizer': args.normalizer,
                        'source_set_id': args.source_set_id or None})
    print(format_parameter_line(resolved.applied))
    reports = build_report('detection_sweep', database_url, manager, resolved.params)
    for index, report in enumerate(reports):
        if index:
            print()
        print(format_detection_sweep_report(report))


if __name__ == '__main__':
    main()
