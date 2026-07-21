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
from typing import Any, Dict, List, Optional, Tuple

import psycopg

from finiexragengine.exceptions.ragengine_errors import VectorStoreError
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
    skipped_open: List[str] = field(default_factory=list)   # bucket names still growing
    total_lines: int = 0


class OutcomeArchiveExporter:
    """Reads `outcomes` and writes the rotated JSONL archive layout. Pure read + file write."""

    def __init__(self, database_url: str, table: str = 'outcomes') -> None:
        self._database_url = database_url
        self._table = table

    def export(self, out_dir: Path, *, boundary: Boundary = 'daily',
               pipeline: Optional[str] = None, day: Optional[str] = None,
               include_open: bool = False,
               now: Optional[datetime] = None) -> ExportResult:
        """Write one JSONL file per (stream, closed bucket) under `out_dir`.

        Args:
            out_dir: archive root; files land at `<out_dir>/<stream_id>/<bucket>.jsonl`.
            boundary: 'daily' | 'weekly' bucket size.
            pipeline: restrict to one stream id (default: every stream in the store).
            day: restrict to the bucket a given `YYYY-MM-DD` falls into (default: all).
            include_open: also write the current, still-growing bucket (NOT redundancy-safe
                — a later run rewrites it; use only for a throwaway peek).
            now: reference time for the closed-bucket cut (default: wall-clock UTC).
        """
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current = bucket_name(now, boundary)
        target = bucket_name(_parse_day(day), boundary) if day else None

        buckets = self._grouped_lines(boundary, pipeline)   # {(stream, bucket): [line, ...]}
        result = ExportResult()
        for (stream, bucket), lines in sorted(buckets.items()):
            if target is not None and bucket != target:
                continue
            # A bucket is closed once the clock has moved past it (sortable names ⇒ a plain
            # comparison is chronological). The open one is skipped unless asked for.
            if bucket >= current and not include_open:
                if bucket not in result.skipped_open:
                    result.skipped_open.append(bucket)
                continue
            path = out_dir / stream / f'{bucket}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join(json.dumps(line) + '\n' for line in lines),
                            encoding='utf-8')
            result.files.append(ExportedFile(path, stream, bucket, len(lines)))
            result.total_lines += len(lines)
        result.skipped_open.sort()
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


def _parse_day(day: str) -> datetime:
    parsed = datetime.fromisoformat(day)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
