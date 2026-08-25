"""Detection-threshold preflight (ISSUE_106) — can these thresholds still fire?

`DetectionConfig` carries three thresholds that only mean something **relative to the feeds that
are actually running**, and nothing validated them against the source set. The set is declared once
and then erodes at runtime in two independent ways the config never learns about: `enabled: false`
(usually per machine, because reachability is an environment fact) and the quarantine ladder
switching a failing feed out dynamically.

The idiom is ordinary configuration preflight — `nginx -t`, `promtool check rules`, a Kubernetes
admission webhook: the config is checked against the world it will run in *before* it runs, and an
unsatisfiable rule is reported rather than silently never firing. The house precedents are the
corpus embedding-model guard (ISSUE_16) and the `[OVERRIDE]` / `[AUTH]` startup reports.

**Warn, never refuse.** A pending migration is corruption and rightly blocks boot; an
over-ambitious threshold is a *degraded feature*, and blocking on it would take the engine down
over a quarantined feed. Warning it is the whole point — the failure mode today is silence.

**Two checks, deliberately different in strength** (see `DetectionReachability`): the weight check
is a proof, the cluster check is an indicator, because `count_neighbors` counts corpus articles
rather than distinct feeds. The wording keeps them apart on purpose — reporting an indicator as a
proof is how a report loses its credibility.

**Quarantine is deliberately not part of the boot check**: it is dynamic, so a boot-time verdict
would be stale within the hour. The boot line reports the `enabled` count; the `breaking` report
reads the effective count at read time. Two honestly different numbers, and both say which they are.
"""
import logging
from dataclasses import replace
from typing import List, Set

from finiexragengine.types.config_types.source_set_types import SourceSetConfig
from finiexragengine.types.ingest_types import DetectionReachability

logger = logging.getLogger(__name__)

# How many switched-off feeds the line names before it collapses to `+N more`.
_NAMED_DISABLED = 4


def _count_label(reach: DetectionReachability) -> str:
    """Name the population the verdict was taken against, so the claim is checkable."""
    return 'pollable feed count' if reach.quarantine_known else 'active feed count'


def check_detection_reachability(source_set: SourceSetConfig) -> DetectionReachability:
    """Measure one set's thresholds against the feeds that actually run.

    Pure — no DB, no network, no clock. `active_sources()` is the one definition of "the feeds that
    run", read by the ingestor and `SourceReach` too, so this guard cannot drift from what happens.
    """
    detection = source_set.detection
    active = source_set.active_sources()
    weights = [source.weight for source in active]
    return DetectionReachability(
        source_set_id=source_set.source_set_id,
        declared=len(source_set.sources),
        active=len(active),
        disabled_ids=[source.source_id for source in source_set.sources if not source.enabled],
        active_ids=[source.source_id for source in active],
        mid_cluster_size=detection.mid_cluster_size,
        high_cluster_size=detection.high_cluster_size,
        keyword_source_weight=detection.keyword_source_weight,
        max_active_weight=max(weights) if weights else 0.0,
        at_or_above_gate=sum(1 for weight in weights
                             if weight >= detection.keyword_source_weight),
    )


def with_quarantine(reach: DetectionReachability,
                    quarantined_ids: Set[str]) -> DetectionReachability:
    """Re-state a config-time verdict against the feeds that are pollable *right now*.

    Quarantine is deliberately outside the boot check — it is dynamic, so a boot-time verdict would
    be stale within the hour. This is the other half: a read-time surface hands in who is currently
    switched out, and the verdict is recomputed against `effective` instead of `active`. The result
    carries `quarantine_known`, so the two numbers can never be mistaken for each other.

    Only ids belonging to THIS set are kept — the health store is engine-wide, and counting another
    set's quarantine here would understate this set's reach.
    """
    mine = sorted(source_id for source_id in quarantined_ids
                  if source_id in reach.active_ids)
    return replace(reach, quarantined_ids=mine, quarantine_known=True)


def format_reachability_lines(reach: DetectionReachability, *, prefix: str = '') -> List[str]:
    """The operator-facing wording, shared by the boot log and the `breaking` report.

    One function so the two surfaces cannot describe the same state differently — the divergence
    ISSUE_82 removed one domain over. `prefix` lets the boot log tag its lines `[DETECTION]` while
    the report renders them bare.
    """
    tag = f'{prefix}{reach.source_set_id}'
    # Named, but capped — same idiom as the `[OVERRIDE]` line's `+N more`. A catalogue carrying
    # eighteen parked candidates (ISSUE_107) would otherwise bury the number that matters in a
    # list nobody reads.
    out_note = ''
    if reach.disabled_ids:
        shown = reach.disabled_ids[:_NAMED_DISABLED]
        rest = len(reach.disabled_ids) - len(shown)
        out_note = (f', {len(reach.disabled_ids)} out: {", ".join(shown)}'
                    + (f' +{rest} more' if rest else ''))
    # Which count the verdict below was taken against — the whole point of the boot/read-time
    # split. Saying "active feeds" on both surfaces would present a boot-time claim as a live one.
    if reach.quarantine_known:
        quarantined = (f' · {len(reach.quarantined_ids)} quarantined right now: '
                       f'{", ".join(reach.quarantined_ids)}' if reach.quarantined_ids else
                       ' · none quarantined right now')
        lines = [f'{tag} · {reach.effective} of {reach.active} enabled feeds pollable '
                 f'({reach.declared} declared{out_note}){quarantined}']
    else:
        lines = [f'{tag} · {reach.active} active feeds ({reach.declared} declared{out_note}) '
                 f'· quarantine not included (it is dynamic — the breaking report reads it live)']

    # The cluster half — an indicator, and worded as one. "Only intra-feed duplication can get
    # there" is the true statement; "unreachable" would be false, and falsely reassuring.
    if reach.cluster_needs_self_duplication:
        lines.append(
            f'{tag} · high_cluster_size={reach.high_cluster_size} exceeds the '
            f'{_count_label(reach)} ({reach.effective}) — the cross-feed path to HIGH cannot be '
            f'reached by these feeds alone; only a feed duplicating itself, or the keyword path, '
            f'can still fire')
    if reach.mid_needs_self_duplication:
        lines.append(
            f'{tag} · mid_cluster_size={reach.mid_cluster_size} exceeds the '
            f'{_count_label(reach)} ({reach.effective}) — same for MID: no cross-feed route left')

    # The weight half — a proof, and it is the one that fails *silently*, because a keyword hit
    # that never fires leaves nothing behind at all.
    if reach.keyword_path_dead:
        lines.append(
            f'{tag} · keyword_source_weight={reach.keyword_source_weight} is above every active '
            f'feed (highest {reach.max_active_weight}) — the keyword fast-path CANNOT fire; '
            f'detection is cluster-only')
    else:
        # Not a warning: the distribution is what ISSUE_82 wanted and could not get. A gate that
        # 11 of 11 feeds clear is not a trust scale, it is a constant with extra steps.
        lines.append(
            f'{tag} · keyword gate {reach.keyword_source_weight} · '
            f'{reach.at_or_above_gate} of {reach.active} active feeds at or above '
            f'(highest {reach.max_active_weight})')
    if reach.satisfiable:
        lines.append(f'{tag} · cluster thresholds {reach.mid_cluster_size}/'
                     f'{reach.high_cluster_size} satisfiable by {reach.effective} '
                     f'{"pollable" if reach.quarantine_known else "active"} feeds')
    return lines


def log_detection_preflight(source_sets: List[SourceSetConfig]) -> List[DetectionReachability]:
    """Run the preflight over every set at boot and report it, leaf by leaf.

    Returns the results so a caller can surface them too — a guard that only logs is a guard whose
    finding scrolls away.
    """
    results = [check_detection_reachability(source_set) for source_set in source_sets]
    for reach in results:
        for line in format_reachability_lines(reach, prefix=''):
            # Unsatisfiable is a degraded feature, so it warns; the rest is the resolved
            # configuration reported for the record, like `[OVERRIDE]` and `[AUTH]`.
            log = logger.warning if not reach.satisfiable else logger.info
            log('[DETECTION] %s', line)
    return results
