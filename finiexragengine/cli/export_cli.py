"""CLI entry point: export persisted outcomes to the rotated JSONL archive (ISSUE_13).

The manual handover / backfill path — reads the engine's `outcomes` journal and writes the
collector's bucketed layout (`<stream>/<bucket>.jsonl`) for a consumer (Testing IDE, #141).
Closed buckets only; the DB `archive_export_log` flag records what was already handed over.

Exactly one scope is required (no default) — a bare run is an error, on purpose: it prevents
silently re-writing the whole history as it grows.
"""
import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from finiexragengine.core.outcome.outcome_exporter import OutcomeArchiveExporter
from finiexragengine.utils.console_encoding import use_utf8_output


def _resolve_since(value: str) -> str:
    """A date `YYYY-MM-DD`, or the keyword `week` → Monday of the current ISO week (UTC)."""
    if value == 'week':
        today = datetime.now(timezone.utc).date()
        return (today - timedelta(days=today.weekday())).isoformat()
    return value


def main() -> None:
    # Reports carry `→`, `⚠`, `—`; a piped run would die on a cp1252 stdout.
    use_utf8_output()
    parser = argparse.ArgumentParser(
        description='Export persisted outcomes to the rotated JSONL archive layout')
    parser.add_argument('--out', default='data/signal_export',
                        help='archive root; files land at <out>/<stream_id>/<bucket>.jsonl')
    parser.add_argument('--boundary', choices=['daily', 'weekly'], default='daily',
                        help='bucket size (default daily)')
    parser.add_argument('--pipeline', default=None,
                        help='restrict to one stream id (default: every stream in the store)')
    parser.add_argument('--include-open', action='store_true',
                        help='also write the current, still-growing bucket — a peek; not '
                             'redundancy-safe and never flagged')

    # Exactly one scope selector, no default: a bare run errors instead of silently re-exporting
    # the whole history. --incremental reads the DB flag (only new days); the others ignore it
    # for selection but still flag what they write.
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--incremental', action='store_true',
                       help='only closed days not yet exported (reads the archive_export_log flag)')
    scope.add_argument('--since', metavar='DATE|week',
                       help="whole buckets on/after a YYYY-MM-DD (or 'week' = this ISO week)")
    scope.add_argument('--all', action='store_true', dest='all_time',
                       help='every closed day (all-time) — the deliberate full re-export')
    scope.add_argument('--day', help='the single bucket a YYYY-MM-DD falls into')

    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is not set (point it at the pgvector Postgres)')

    # --all needs no narrowing (all closed days is the base behaviour); the other scopes narrow.
    since = _resolve_since(args.since) if args.since else None
    exporter = OutcomeArchiveExporter(database_url)
    result = exporter.export(Path(args.out), boundary=args.boundary, pipeline=args.pipeline,
                             day=args.day, since=since, incremental=args.incremental,
                             include_open=args.include_open)

    for exported in result.files:
        print(f'wrote {exported.lines:>4} lines · {exported.path}')
    if result.skipped_open:
        # Say WHEN it closes, not only that it is open. Buckets are UTC and an operator's clock
        # usually is not, so "still growing: 2026-08-28" read at 00:59 local (22:59 UTC) looks
        # exactly like an export that has fallen a day behind — which is how it was read on
        # 2026-08-28. The remaining minutes are the part that ends the question.
        print(f'skipped {len(result.skipped_open)} open bucket(s) (still growing): '
              + ', '.join(result.skipped_open))
        if result.open_closes_at is not None:
            remaining = result.open_closes_at - datetime.now(timezone.utc)
            minutes = max(0, int(remaining.total_seconds() // 60))
            print(f'   closes {result.open_closes_at:%Y-%m-%dT%H:%MZ} '
                  f'(in {minutes // 60}h {minutes % 60}m) — buckets are UTC, '
                  f'so a local clock may already show the next day')
        print('   export once closed; --include-open is a peek, not a handover '
              '(it is deliberately not flagged as exported)')
    if result.skipped_flagged:
        print(f'skipped {len(result.skipped_flagged)} already-exported bucket(s) (incremental)')
    if not result.files:
        print('nothing to export (no buckets matched this scope)')
    else:
        print(f'--- exported {result.total_lines} lines across '
              f'{len(result.files)} file(s) → {args.out}')


if __name__ == '__main__':
    main()
