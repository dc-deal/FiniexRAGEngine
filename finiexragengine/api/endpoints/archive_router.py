"""`GET /v1/pipelines/{pipeline_id}/archive` — the series in a bounded time window, as NDJSON.

**Its own address because it is its own question.** `/envelopes` answers "what did I miss" inside
the stream's replay window (24 h, bounded on purpose to protect the live stream); this answers "what
did the series say between two instants" for any time the journal still holds — the path a
retrospective takes instead of a manual `export_cli` run and a file copy, and the way to read the
current day while it is still growing. Beside it, `/archive/days` says where the series starts and
how big each day is, before anything is pulled.

Three properties are the design, each deliberate:

- **Bounded twice.** A window spans at most `max_span_hours` and holds at most `max_lines` lines
  (measured 2026-09-13: ~148 envelopes and 5–6 MB per pipeline-day). A request is answered in full
  or refused with the count — never cut, so a partial answer can never be mistaken for the whole.
- **The export's line, byte for byte.** Built by the exporter's own line builder, so a full UTC day
  read here equals that day's exported file.
- **Read-only against the handover.** Nothing here calls `export()` or writes `archive_export_log`;
  the incremental export keeps deciding from its own record, and `/archive/days` only READS that
  record to show which days it has taken.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Query, Response, Security

from finiexragengine.core.outcome.outcome_exporter import OutcomeArchiveExporter
from finiexragengine.core.pipeline.pipeline_registry import PipelineRegistry
from finiexragengine.exceptions.ragengine_errors import FiniexRagError, PipelineNotFoundError
from finiexragengine.types.api_types import ArchiveDayEntry, ArchiveDays
from finiexragengine.utils.archive_layout import bucket_name

logger = logging.getLogger(__name__)


def _utc(moment: datetime) -> datetime:
    """A naive instant is read as UTC — the archive's only time base."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_archive_router(exporter: OutcomeArchiveExporter, registry: PipelineRegistry,
                         grant: Optional[Callable[..., None]] = None,
                         max_span_hours: int = 24, max_lines: int = 500,
                         now: Callable[[], datetime] = _utc_now) -> APIRouter:
    """Build the archive router. `scopes=['pipelines']` — the same series `/latest`, `/envelopes`
    and the stream already serve to that grant, over a longer range; no new data class is exposed.

    `now` is injectable so a test can place a window in the present without a sleep.
    """
    guards = [Security(grant, scopes=['pipelines'])] if grant is not None else []
    router = APIRouter(prefix='/v1/pipelines', tags=['pipelines'], dependencies=guards)

    def _require_pipeline(pipeline_id: str) -> None:
        try:
            registry.get(pipeline_id)
        except PipelineNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.get('/{pipeline_id}/archive/days', response_model=ArchiveDays)
    def archive_days(pipeline_id: str) -> ArchiveDays:
        """Lines per UTC day, oldest first — where the series starts, before anything is pulled."""
        _require_pipeline(pipeline_id)
        try:
            days = exporter.day_counts(pipeline_id)
        except FiniexRagError as exc:
            logger.warning('[ARCHIVE] %s day index could not be read: %s', pipeline_id, exc)
            raise HTTPException(status_code=503, detail=f'journal unavailable: {exc}')
        # The still-growing day is today's UTC bucket — the boundary the export skips as open.
        today = bucket_name(now(), 'daily')
        return ArchiveDays(pipeline_id=pipeline_id, days=[
            ArchiveDayEntry(day=day.day, lines=day.lines, open=day.day >= today,
                            exported=day.exported)
            for day in days])

    @router.get('/{pipeline_id}/archive')
    def archive(pipeline_id: str,
                start: datetime = Query(..., alias='from',
                                        description='window start, inclusive (UTC)'),
                end: datetime = Query(..., alias='to',
                                      description='window end, exclusive (UTC)')) -> Response:
        """One stream's archive lines with `from <= ts < to`, oldest first, as NDJSON."""
        _require_pipeline(pipeline_id)
        start, end = _utc(start), _utc(end)
        if end <= start:
            raise HTTPException(status_code=400,
                                detail='the window end (to) must be after its start (from)')
        span_hours = (end - start) / timedelta(hours=1)
        if span_hours > max_span_hours:
            raise HTTPException(status_code=422, detail={
                'code': 'span_too_long', 'max_span_hours': max_span_hours,
                'requested_hours': round(span_hours, 2)})
        try:
            window = exporter.lines_between(pipeline_id, start, end, max_lines)
        except FiniexRagError as exc:
            logger.warning('[ARCHIVE] %s window could not be read: %s', pipeline_id, exc)
            raise HTTPException(status_code=503, detail=f'journal unavailable: {exc}')
        if window.exceeded:
            # Refused rather than cut: a partial answer is the one a caller can mistake for the
            # whole. The count tells them by how much to narrow the window.
            raise HTTPException(status_code=422, detail={
                'code': 'too_many_lines', 'max_lines': max_lines,
                'lines_at_least': max_lines + 1, 'hint': 'narrow the window'})
        body = ''.join(json.dumps(line) + '\n' for line in window.lines)
        return Response(content=body, media_type='application/x-ndjson', headers={
            'X-Archive-Lines': str(len(window.lines)),
            'X-Archive-From': start.isoformat(),
            'X-Archive-To': end.isoformat(),
            # A window reaching into the present holds data that is still growing — said, not
            # left for the caller to infer from the clock.
            'X-Archive-Window-Open': 'true' if end > now() else 'false'})

    return router
