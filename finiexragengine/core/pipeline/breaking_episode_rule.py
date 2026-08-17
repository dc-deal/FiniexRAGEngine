"""When a breaking story starts, continues and ends (ISSUE_82) — the one rule both surfaces drive.

`is_breaking` is a per-pass threshold verdict (`urgency >= breaking.urgency_threshold`), and the
LLM's `urgency` is quantised: measured over seven days it emits seven values, never 0.65/0.75/0.85.
The gate at 0.8 therefore sits exactly on a populated lattice point with the largest non-zero
bucket (0.7) one step below it — 70% of all non-zero scores sit on the pair straddling the
threshold. Mean pass-to-pass drift on a byte-identical source set is 0.032, a third of a step, and
that is enough to flip the verdict: on 2026-08-17 XRPUSD crossed the threshold nine times in
fifteen passes while `signal` stayed BUY 15/15 and the freshest source never moved.

Counting an episode per rising edge turned ~14 stories into 66 episodes in a week, and corrupted
the reaction metric on top (each re-trigger re-samples against an ageing article). The fix is the
standard answer to a noisy signal crossing a threshold — a **Schmitt trigger**: open high, hold
low. An episode opens on the recorded breaking verdict, stays open while `urgency` holds at or
above a lower exit threshold, and closes only after the gap elapses with neither condition met.

Two design points worth keeping:

- **Opening uses the RECORDED `is_breaking`, never a re-derivation from today's threshold.** An
  archived pass keeps the verdict its pipeline actually took, so retuning `urgency_threshold` later
  cannot silently rewrite history when the store report re-groups it. `urgency` is consulted only
  for the *hold* condition, which also makes legacy rows (urgency defaulting to 0.0) degrade to the
  pre-ISSUE_82 behaviour rather than misbehave.
- **The rule owns its state.** Both callers used to group episodes themselves, and the two
  derivations silently diverged for weeks (see `breaking_report`). One stateful rule object driven
  by two thin callers removes that class of bug rather than documenting around it.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional

from finiexragengine.types.config_types.pipeline_config_types import PipelineConfig
from finiexragengine.types.eval_types import BreakingPassDecision

# Mirrors `BreakingConfig` in types/config_types/pipeline_config_types.py — the config is the
# truth, these keep a bare `BreakingEpisodeRule()` (tests, legacy call sites) meaningful.
DEFAULT_EXIT_THRESHOLD = 0.7
DEFAULT_EPISODE_GAP = timedelta(minutes=45)


@dataclass
class _OpenEpisode:
    """One currently-open episode's state — file-private, never crosses a seam."""
    started_at: datetime
    last_qualifying: datetime    # last pass that opened or held it; the gap measures from here


class BreakingEpisodeRule:
    """Decides, pass by pass, whether a symbol is starting, continuing or leaving an episode.

    Stateful and **driven in timestamp order** — the live tracker feeds it each envelope as it is
    produced, the store report replays persisted envelopes ordered by `ts`. Both therefore see the
    identical decision for identical input, which is the property the two hand-rolled groupings
    could never guarantee.

    Keyed on the caller's choice of key. Both callers pass `base_currency or symbol`, so a query
    group's fanned symbols (ETHUSD/ETHEUR, both base ETH) are one analysis and one episode
    (ISSUE_70) rather than two.
    """

    def __init__(self, exit_threshold: float = DEFAULT_EXIT_THRESHOLD,
                 gap: timedelta = DEFAULT_EPISODE_GAP) -> None:
        self._exit_threshold = exit_threshold
        self._gap = gap
        self._open: Dict[str, _OpenEpisode] = {}

    def observe(self, key: str, ts: datetime, is_breaking: bool,
                urgency: float = 0.0) -> BreakingPassDecision:
        """Fold one pass into the episode state and report what it did."""
        # Close first, decide second: an episode whose gap ran out ended BEFORE this pass, so a
        # breaking pass arriving after it opens a genuinely new one rather than resurrecting the
        # old. Doing this on read (rather than on a timer) keeps the rule pure — it needs no clock
        # of its own, which is what lets the batch report replay history through the same code.
        open_episode = self._open.get(key)
        if open_episode is not None and (ts - open_episode.last_qualifying) > self._gap:
            del self._open[key]
            open_episode = None

        # The two conditions of the Schmitt trigger. `is_breaking` also holds, so a pass at or
        # above the OPEN threshold never fails to keep its own episode alive.
        holds = is_breaking or urgency >= self._exit_threshold

        if open_episode is None:
            if not is_breaking:
                return BreakingPassDecision(opened=False, held=False, in_episode=False)
            self._open[key] = _OpenEpisode(started_at=ts, last_qualifying=ts)
            return BreakingPassDecision(opened=True, held=False, in_episode=True, started_at=ts)

        # An episode is running. A qualifying pass advances the gap anchor; a dip leaves the anchor
        # where it was — the episode stays open, but its clock keeps running toward the gap.
        if holds:
            open_episode.last_qualifying = ts
        return BreakingPassDecision(opened=False, held=holds, in_episode=True,
                                    started_at=open_episode.started_at)

    def is_open(self, key: str) -> bool:
        """Whether an episode is currently open for this key (no clock consulted)."""
        return key in self._open

    def get_gap(self) -> timedelta:
        """The configured gap — surfaces render live-vs-ended against the same value."""
        return self._gap

    def get_exit_threshold(self) -> float:
        return self._exit_threshold


# The class's config constructors. They live here rather than at each call site because four
# places need the same mapping (the assembler, the store report, the weekly report, the CLI) and
# a second reading of `breaking.*` is exactly how the two groupings drifted apart before.
# Deliberately typed against `PipelineConfig`, not the registry: the registry reaches
# Pipeline -> PipelineRunner, and this module is imported from inside that graph.

def rule_from_config(config: PipelineConfig) -> BreakingEpisodeRule:
    """One pipeline's episode rule, from its `breaking` block."""
    return BreakingEpisodeRule(exit_threshold=config.breaking.urgency_exit_threshold,
                               gap=timedelta(minutes=config.breaking.episode_gap_minutes))


def rules_from_configs(configs: Iterable[PipelineConfig]) -> Dict[str, BreakingEpisodeRule]:
    """`pipeline_id -> rule` for the store-side reports, which group many pipelines at once.

    Callers pass `[p.get_config() for p in registry.list_pipelines()]` — the registry must come
    from `AppConfigManager.build_pipeline_registry()`, the only load path that applies the
    `user_configs/` overlay. A pipeline_id in the archive but not in this map is an orphan (a
    retired stream) and the report falls back to the schema defaults for it.
    """
    return {config.pipeline_id: rule_from_config(config) for config in configs}
