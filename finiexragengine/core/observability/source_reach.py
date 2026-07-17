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
from typing import List

from finiexragengine.core.observability.source_health_store import SourceHealthStore
from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import ReachCensus


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
        configured_ids = {source.source_id for source in active}
        reached_ids = self._health_store.reach_of(configured_ids)
        # Report the gap in the set's declared order — a stable list reads the same run to run.
        unreached: List[str] = [source.source_id for source in active
                                if source.source_id not in reached_ids]
        return ReachCensus(configured=len(active),
                           reached=len(configured_ids & reached_ids),
                           unreached=unreached)
