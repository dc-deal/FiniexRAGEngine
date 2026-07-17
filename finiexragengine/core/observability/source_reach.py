"""Source reach — the one place a set's config and its feed health are combined.

Answers a single question for the envelope: *of the sources this set is configured to run, how
many are actually delivering right now?* The two halves live in different layers and neither can
answer alone:

- **Config** (`SourceSetConfig.active_sources`) knows which feeds are switched on. It cannot know
  whether they answer.
- **Health** (`SourceHealthStore`) knows which feeds answered their last poll and which are in
  cool-off. It has no notion of `enabled` — that is a config fact with no column in the store.

They were never combined, which is why `sources_reached` used to be `configured - failed`: a
subtraction the runner could only make from its *own* pass. In worker mode it has no pass at all
(the ingest worker owns acquisition), so the number was `configured` every single run — a full
reach the run never attempted. Reading health instead works in both modes, because acquisition
records every poll there regardless of who ran it (CLAUDE.md — capture at the call, report from
the store).

A source within its own poll floor needs no special case here: a floor skip deliberately records
no health, so the source keeps the verdict of its last real poll — correct, because its articles
are in the corpus either way.
"""
from datetime import datetime, timezone
from typing import List, Optional

from finiexragengine.core.observability.source_health_store import SourceHealthStore
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import ReachCensus, SourceHealthState, UnreachedSource


class SourceReach:
    """Counts a source-set's live sources — config ∩ health, resolved per call."""

    def __init__(self, source_set: SourceSetConfig, health_store: SourceHealthStore) -> None:
        self._source_set = source_set
        self._health_store = health_store

    def census(self) -> ReachCensus:
        """The current reach of this set — one live health query, resolved fresh per run.

        A disabled source appears in neither number: it is not configured to run, so it is not
        a source the run failed to reach. Counting it in both (as a whole-catalogue count would)
        would claim a feed's contribution that never existed.
        """
        active = self._source_set.active_sources()
        states = self._health_store.states_of({source.source_id for source in active})
        # Walk the config, not the health rows — the declared order is stable, and a source with
        # no row at all must still get a verdict rather than fall out of the census.
        unreached: List[UnreachedSource] = []
        for source in active:
            state = states.get(source.source_id)
            if state is not None and state.delivering:
                continue
            unreached.append(UnreachedSource(source.source_id, _reason(state)))
        return ReachCensus(configured=len(active),
                           reached=len(active) - len(unreached),
                           unreached=unreached)


def _reason(state: Optional[SourceHealthState]) -> str:
    """Why a source is not delivering, in words — this text lands in the envelope.

    The Sources report shows *now*; the envelope preserves *then*. A run persisted today must
    still explain itself on replay tomorrow, long after the live health row has moved on, so the
    cause travels with the outcome rather than only with the store.
    """
    if state is None:
        return 'never polled'
    last = ' '.join(part for part in (state.last_error_type,
                                      str(state.last_status) if state.last_status else '')
                    if part) or 'unknown error'
    # Only a cool-off that still has time left is one: an *elapsed* quarantine keeps its
    # `quarantined_until` in the row until a successful poll clears it, so testing the column for
    # presence alone would report a date in the past as if the feed were still held back. Such a
    # source is retryable and simply has not succeeded yet — that is a failing feed, not a held one.
    if (state.quarantined_until is not None
            and state.quarantined_until > datetime.now(timezone.utc)):
        return (f'quarantined until {state.quarantined_until:%m-%d %H:%M} UTC '
                f'({state.consecutive_failures} consecutive failures, last {last})')
    return f'last poll failed ({last}, {state.consecutive_failures} consecutive)'
