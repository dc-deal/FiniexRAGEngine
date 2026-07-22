"""Export persisted outcomes to the rotated JSONL archive (ISSUE_13 — handover path).

The durable live archive is the collector's job (ISSUE_9); this is the *manual* handover /
backfill path that reads the engine's own immutable journal (`outcomes`) and writes the
**same** bucketed layout (`<stream>/<bucket>.jsonl`) — so a day of real signals can be
handed to a consumer (the Testing IDE, #141) without the collector running yet.

Two properties make it redundancy-proof, which is the whole point:

- **Closed buckets only.** A line lands in the bucket of its `collected_msc`; the current
  (still-growing) bucket is skipped unless explicitly asked for (`include_open`). A day
  handed over is therefore frozen — never re-emitted with different content.
- **Idempotent full rewrite.** Each bucket file is rewritten in full from the journal,
  ordered by (ts, id); a closed day never gains rows, so re-running yields a byte-identical
  file. No append, no dedup bookkeeping — the journal is the single source.

`collected_msc` here is the envelope's analysis `timestamp` in epoch-ms: there is no
collector receive-time in a DB export, and this matches the mock the IDE validated against.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
from finiexragengine.types.config_types.app_config_types import WeeklyReportConfig
from finiexragengine.utils.archive_layout import Boundary, bucket_name


@dataclass
class ExportedFile:
    """One written bucket file — the handover unit."""
    path: Path
    stream_id: str
    bucket: str
    lines: int


@dataclass
class ExportResult:
    """What the export produced — a typed result, not a bare list (stage-boundary rule)."""
    files: List[ExportedFile] = field(default_factory=list)
    skipped_open: List[str] = field(default_factory=list)      # bucket names still growing
    skipped_flagged: List[str] = field(default_factory=list)   # 'stream/bucket' already exported
    total_lines: int = 0


class OutcomeArchiveExporter:
    """Reads `outcomes` and writes the rotated JSONL archive layout. Pure read + file write."""

    def __init__(self, database_url: str, table: str = 'outcomes') -> None:
        self._database_url = database_url
        self._table = table

    def export(self, out_dir: Path, *, boundary: Boundary = 'daily',
               pipeline: Optional[str] = None, day: Optional[str] = None,
               since: Optional[str] = None, incremental: bool = False,
               include_open: bool = False,
               now: Optional[datetime] = None) -> ExportResult:
        """Write one JSONL file per (stream, closed bucket) under `out_dir`.

        The scope selectors narrow *which* closed buckets are written; every written closed
        bucket is then flagged in `archive_export_log` (the "already handed over" record).

        Args:
            out_dir: archive root; files land at `<out_dir>/<stream_id>/<bucket>.jsonl`.
            boundary: 'daily' | 'weekly' bucket size.
            pipeline: restrict to one stream id (default: every stream in the store).
            day: restrict to the bucket a given `YYYY-MM-DD` falls into.
            since: lower bound — only buckets on/after the one `YYYY-MM-DD` falls into (whole
                buckets, so a mid-day cut can never split one).
            incremental: skip closed buckets already flagged in `archive_export_log` (only the
                not-yet-exported ones). The other selectors ignore the flag but still set it.
            include_open: also write the current, still-growing bucket — a throwaway peek, NOT
                redundancy-safe and never flagged (a later run rewrites the then-closed day).
            now: reference time for the closed-bucket cut (default: wall-clock UTC).
        """
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current = bucket_name(now, boundary)
        target = bucket_name(_parse_day(day), boundary) if day else None
        since_bucket = bucket_name(_parse_day(since), boundary) if since else None

        buckets = self._grouped_lines(boundary, pipeline)   # {(stream, bucket): [line, ...]}
        # Incremental consults the flag; every other mode ignores it for *selection* (but still
        # writes it below), so a re-export of an already-handed-over day stays possible on demand.
        flagged = self._flagged_buckets(boundary) if incremental else set()

        result = ExportResult()
        written_flags: List[Tuple[str, str, int]] = []       # (stream, bucket, lines) to flag
        for (stream, bucket), lines in sorted(buckets.items()):
            if target is not None and bucket != target:
                continue
            if since_bucket is not None and bucket < since_bucket:
                continue
            # A bucket is closed once the clock has moved past it (sortable names ⇒ a plain
            # comparison is chronological). The open one is skipped unless asked for.
            is_open = bucket >= current
            if is_open and not include_open:
                if bucket not in result.skipped_open:
                    result.skipped_open.append(bucket)
                continue
            if incremental and not is_open and (stream, bucket) in flagged:
                result.skipped_flagged.append(f'{stream}/{bucket}')
                continue
            path = out_dir / stream / f'{bucket}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(json.dumps(line) + '\n' for line in lines),
                            encoding='utf-8')
            result.files.append(ExportedFile(path, stream, bucket, len(lines)))
            result.total_lines += len(lines)
            # A finished (closed) handover is flagged; an --include-open peek is not — the day
            # still grows, and its real, frozen export comes once it closes.
            if not is_open:
                written_flags.append((stream, bucket, len(lines)))

        if written_flags:
            self._flag_exported(boundary, written_flags)
        result.skipped_open.sort()
        result.skipped_flagged.sort()
        return result

    def _grouped_lines(self, boundary: Boundary, pipeline: Optional[str],
                       ) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        """Read the journal (oldest first) and fold rows into per-(stream, bucket) lines."""
        where, params = ('', [])
        if pipeline is not None:
            where, params = ('WHERE pipeline_id = %s', [pipeline])
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                # No table yet = nothing produced; an empty export, not a crash.
                cur.execute('SELECT count(*) FROM information_schema.tables '
                            'WHERE table_name = %s', (self._table,))
                if cur.fetchone()[0] == 0:
                    return {}
                cur.execute(
                    f'SELECT pipeline_id, ts, envelope FROM {self._table} {where} '
                    'ORDER BY pipeline_id, ts, id', params)
                rows = cur.fetchall()
        except psycopg.Error as exc:
            raise VectorStoreError(f'outcome export failed: {exc}') from exc

        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for pipeline_id, ts, envelope in rows:
            env = envelope if isinstance(envelope, dict) else json.loads(envelope)
            ts = ts.astimezone(timezone.utc)
            # collected_msc = analysis time in epoch-ms (no collector receive-time exists
            # for a DB export; consistent with the validated mock). Prepended so the line
            # is exactly `{collected_msc, ...envelope}` — the shape #9/#141 expect.
            line = {'collected_msc': int(ts.timestamp() * 1000), **env}
            grouped.setdefault((pipeline_id, bucket_name(ts, boundary)), []).append(line)
        return grouped

    def _flagged_buckets(self, boundary: Boundary) -> Set[Tuple[str, str]]:
        """The (stream, bucket) pairs already exported for this boundary — the incremental skip
        set. A missing table (fresh DB, migration pending) means nothing is flagged yet."""
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cur.execute('SELECT count(*) FROM information_schema.tables '
                            'WHERE table_name = %s', ('archive_export_log',))
                if cur.fetchone()[0] == 0:
                    return set()
                cur.execute('SELECT stream_id, bucket FROM archive_export_log '
                            'WHERE boundary = %s', (boundary,))
                return {(row[0], row[1]) for row in cur.fetchall()}
        except psycopg.Error as exc:
            raise VectorStoreError(f'reading archive export log failed: {exc}') from exc

    def _flag_exported(self, boundary: Boundary,
                       written: List[Tuple[str, str, int]]) -> None:
        """Mark each written closed bucket as handed over (upsert — a re-export refreshes it)."""
        try:
            with psycopg.connect(self._database_url) as conn, conn.cursor() as cur:
                cur.executemany(
                    'INSERT INTO archive_export_log (stream_id, bucket, boundary, lines) '
                    'VALUES (%s, %s, %s, %s) '
                    'ON CONFLICT (stream_id, bucket, boundary) '
                    'DO UPDATE SET exported_at = now(), lines = EXCLUDED.lines',
                    [(stream, bucket, boundary, lines) for stream, bucket, lines in written])
        except psycopg.Error as exc:
            raise VectorStoreError(f'writing archive export log failed: {exc}') from exc


def _parse_day(day: str) -> datetime:
    parsed = datetime.fromisoformat(day)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def auto_export_weekly(weekly_cfg: WeeklyReportConfig, database_url: str, *,
                       now: Optional[datetime] = None) -> Optional[ExportResult]:
    """Dump the closed-day archive alongside a weekly report, when enabled (ISSUE_13).

    The shared coupling for the CLI (`report_cli`) and the scheduled weekly (API lifespan). Runs
    **incrementally**: only days that have closed since the last export are written (the
    `archive_export_log` flag), so the weekly never rebuilds the whole history. Whole buckets
    only — byte-identical to a manual `export_cli` run. Returns None when the knob is off, so the
    caller can stay silent.
    """
    if not weekly_cfg.export_outcomes:
        return None
    # Incremental: the weekly run writes only the days that closed since the last export
    # (via the archive_export_log flag) — never a full-history rebuild.
    return OutcomeArchiveExporter(database_url).export(
        Path(weekly_cfg.export_dir), boundary='daily', incremental=True, now=now)
