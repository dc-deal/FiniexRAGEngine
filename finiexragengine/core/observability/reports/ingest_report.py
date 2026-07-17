"""Ingest-pass report — what one acquisition pass did with every declared source.

The per-pass companion to the Sources health report: that one aggregates the store over time,
this one renders a single pass. Both must agree, so the status vocabulary is deliberately the
same (`QUARANTINED`, a `left`/`until` cool-off cell).

Rendered against the **declared catalogue**, not against what ran: a source that was skipped,
switched off, or never reached still gets a line. A pass that quietly drops a feed from its own
output is how a permanent 403 came to look like a clean run — the operator's console shows every
source and its status, even when the engine's downstream contract deliberately ignores it
(a disabled feed is invisible to the envelope; it is not invisible here).
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import IngestResult, SourceIngest, SourcePoll

# Display labels per poll status. Upper case marks what wants attention (matching the Sources
# report's FLAGGED); a deliberate, benign state stays lower case.
_STATUS_LABELS: Dict[str, str] = {
    'ok': 'ok',
    'failed': 'FAILED',
    'quarantined': 'QUARANTINED',
    'floor_skipped': 'poll floor',
    'suspended': 'SUSPENDED',
}
_DISABLED = 'disabled'
_NOT_POLLED = 'not polled'
# The columns below occupy 62 chars, so the detail cell closes the row at the 88 the Sources
# report also uses — the two ingest surfaces line up in the same terminal.
_CELL_WIDTH = 26


@dataclass
class IngestSourceRow:
    """One declared source's line in the pass table."""
    source_id: str
    status: str                          # display label (see _STATUS_LABELS)
    ingest: Optional[SourceIngest]       # counters — None when the feed was not polled
    detail: str
    until: Optional[datetime] = None     # cool-off end, for a deferred source

    @property
    def polled(self) -> bool:
        return self.ingest is not None


@dataclass
class IngestReport:
    """One pass, rendered per declared source."""
    source_set_id: str
    result: IngestResult
    rows: List[IngestSourceRow]

    @property
    def declared(self) -> int:
        return len(self.rows)

    @property
    def polled(self) -> int:
        return sum(1 for row in self.rows if row.polled)

    def count_status(self, label: str) -> int:
        return sum(1 for row in self.rows if row.status == label)


def build_ingest_report(source_set_id: str, result: IngestResult,
                        source_set: SourceSetConfig) -> IngestReport:
    """Merge the pass's polls onto the declared catalogue, in config order.

    Walking the catalogue (not the polls) is the point: it is what guarantees a declared source
    can never be missing from the table. Three ways a source has no poll — switched off, or the
    pass aborted before reaching it (a mid-pass budget suspend) — each get their own honest label
    rather than silently vanishing.
    """
    polls: Dict[str, SourcePoll] = {poll.source_id: poll for poll in result.polls}
    rows: List[IngestSourceRow] = []
    for source in source_set.sources:
        poll = polls.get(source.source_id)
        if poll is not None:
            rows.append(IngestSourceRow(source.source_id, _STATUS_LABELS[poll.status],
                                        poll.ingest, poll.detail, poll.until))
        elif not source.enabled:
            # Declared but switched off — never built, so the ingestor never saw it. Its `comment`
            # is the field carrying *why*, so it is the natural detail cell.
            rows.append(IngestSourceRow(source.source_id, _DISABLED, None,
                                        source.comment or 'switched off for this environment'))
        else:
            # Enabled, in the catalogue, yet no poll: the pass stopped early (budget suspend).
            rows.append(IngestSourceRow(source.source_id, _NOT_POLLED, None,
                                        'pass ended before this source was reached'))
    return IngestReport(source_set_id, result, rows)


def _remaining(moment: Optional[datetime]) -> str:
    """Compact time left until a future moment ('21h', '35m', or '0m')."""
    if moment is None:
        return '0m'
    minutes = max(0.0, (moment - datetime.now(timezone.utc)).total_seconds()) / 60
    return f'{minutes / 60:.0f}h' if minutes >= 90 else f'{minutes:.0f}m'


def _detail_cell(row: IngestSourceRow) -> str:
    """The table's short right-hand cell — the full text goes in the notes block below.

    Kept terse on purpose: a `comment` is free prose (a feed's whole history may live there), so
    rendering it inline would blow the row width apart on exactly the feeds worth reading about.
    """
    if row.until is not None:
        return f'{_remaining(row.until)} left'
    return row.detail if len(row.detail) <= _CELL_WIDTH else row.detail[:_CELL_WIDTH - 1] + '…'


def format_ingest_report(report: IngestReport) -> str:
    """Render the pass as the shared console pattern (title + window line + dividers)."""
    divider = '-' * 88
    result = report.result
    # The headline keeps the wording the pass line always had — the cost read stays first.
    lines = [
        f"ingest '{report.source_set_id}': fetched {result.fetched}, "
        f'embedded {result.embedded} (paid), stored {result.stored} new, '
        f'{result.duplicates} duplicates',
    ]
    # A pass-level fact, not a per-source one (ISSUE_47): the circuit-breaker stopped the paid
    # work for everything after the source it tripped on — so it belongs above the table.
    if result.suspended:
        lines.append('  ⏸ paid work suspended (provider quota) — embedding skipped this pass')
    # The window line answers what the old output could not: how many feeds actually ran.
    window = [f'{report.declared} declared', f'{report.polled} polled']
    for label in (_STATUS_LABELS['failed'], _STATUS_LABELS['quarantined'], _DISABLED,
                  _NOT_POLLED, _STATUS_LABELS['floor_skipped']):
        count = report.count_status(label)
        if count:
            window.append(f'{count} {label.lower()}')
    lines.append('sources: ' + ' · '.join(window))
    lines.append(divider)
    lines.append(f'{"source":16} {"status":12} {"fetched":>8} {"embedded":>9} {"new":>5} '
                 f'{"dup":>5}  detail')
    lines.append(divider)
    for row in report.rows:
        if row.ingest is not None:
            counts = (f'{row.ingest.fetched:>8} {row.ingest.embedded:>9} '
                      f'{row.ingest.stored:>5} {row.ingest.duplicates:>5}')
        else:
            # A source that never got polled has no counters — an em dash beats a misleading 0.
            counts = f'{"—":>8} {"—":>9} {"—":>5} {"—":>5}'
        detail = _detail_cell(row)
        lines.append(f'{row.source_id:16.16} {row.status:12} {counts}'
                     + (f'  {detail}' if detail else ''))
    if not report.rows:
        lines.append('(this source-set declares no sources)')
    lines.append(divider)

    # The detail block (same idea as the feed doctor's): everything that did not simply run gets
    # its reason in full — the cut-off `comment` of a disabled feed, an error body, a cool-off
    # end. This is the part that answers "why", and it only lists what is worth reading.
    notes = [row for row in report.rows if row.status != _STATUS_LABELS['ok'] and row.detail]
    if notes:
        lines.append('why sources did not run:')
        for row in notes:
            when = f' (until {row.until:%m-%d %H:%M} UTC)' if row.until is not None else ''
            lines.append(f'  [{row.source_id}] {row.status}{when}: {row.detail}')
        lines.append(divider)
    return '\n'.join(lines)
