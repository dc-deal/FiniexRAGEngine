"""CLI entry point: export persisted outcomes to the rotated JSONL archive (ISSUE_13).

The manual handover / backfill path — reads the engine's `outcomes` journal and writes the
collector's bucketed layout (`<stream>/<bucket>.jsonl`) for a consumer (Testing IDE, #141).
Closed buckets only by default (a handed-over day is frozen — never re-emitted differently).
"""
import argparse
import os
from pathlib import Path

from finiexragengine.core.outcome.outcome_exporter import OutcomeArchiveExporter


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Export persisted outcomes to the rotated JSONL archive layout')
    parser.add_argument('--out', default='data/signal_export',
                        help='archive root; files land at <out>/<stream_id>/<bucket>.jsonl')
    parser.add_argument('--boundary', choices=['daily', 'weekly'], default='daily',
                        help='bucket size (default daily)')
    parser.add_argument('--pipeline', default=None,
                        help='restrict to one stream id (default: every stream in the store)')
    parser.add_argument('--day', default=None,
                        help='restrict to the bucket a YYYY-MM-DD falls into (default: all)')
    parser.add_argument('--include-open', action='store_true',
                        help='also write the current, still-growing bucket — NOT '
                             'redundancy-safe; a later run rewrites it')
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    exporter = OutcomeArchiveExporter(database_url)
    result = exporter.export(Path(args.out), boundary=args.boundary,
                             pipeline=args.pipeline, day=args.day,
                             include_open=args.include_open)

    for exported in result.files:
        print(f'wrote {exported.lines:>4} lines · {exported.path}')
    if result.skipped_open:
        print(f'skipped {len(result.skipped_open)} open bucket(s) (still growing): '
              + ', '.join(result.skipped_open)
              + '  — export once closed, or pass --include-open')
    if not result.files:
        print('nothing to export (no closed buckets matched)')
    else:
        print(f'--- exported {result.total_lines} lines across '
              f'{len(result.files)} file(s) → {args.out}')


if __name__ == '__main__':
    main()
