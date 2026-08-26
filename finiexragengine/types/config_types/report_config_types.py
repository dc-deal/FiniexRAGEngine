"""Per-report configuration (ISSUE_104) — the defaults a call may override.

**The model, and it is the same for every report: config declares, the call overrides.** A window,
a cap or a window *set* is declared here; a CLI flag or an HTTP query parameter narrows it for one
invocation, and the answer says which of the two it used. Before this, the defaults were literals
inside the catalog — invisible to an operator and unreachable from `user_configs/`.

Three classes of parameter, because they are not interchangeable:

- **scope** (`window`, `windows`, and the per-call selectors like `symbol` or `source_id`) — what you
  are looking at. Overridable everywhere.
- **caps** (`recent_problems`, `recent_passes`) — how much you see. Overridable, and bounded on the
  HTTP path like the window is.
- **verdict thresholds** — what the report *calls* good or bad. Config only. A threshold that can be
  set per call means two people read the same report and see different verdicts without either
  knowing why; that is the reason the eval model and the prompt are pipeline-declared rather than
  chosen per request.

**What is deliberately NOT here.** A value the engine itself acts on stays where the engine reads it,
even when a report also uses it:

- `diagnostics.timeout_warn_ratio` — the latency report *and* the weekly report judge against it, so
  it is shared diagnostics policy, not one report's preference;
- `source_health.ladder_reset_hours` — the quarantine ladder itself runs on it. A copy under
  `reports.` would let the report price episodes against a ladder the engine never applied, which is
  precisely the shape of the two-groupings divergence ISSUE_82 removed.

The rule: a report's config object carries the report's own parameters and *reads* engine policy
from where the engine keeps it.
"""
from typing import List

from pydantic import BaseModel, Field


class SourceHealthReportConfig(BaseModel):
    """Rolling state, so no window — the store holds current health, not a series."""
    # Caps the CONSOLE's problem list only. The JSON payload carries the events the store holds;
    # this is how many of them a terminal renders before it stops being an overview.
    recent_problems: int = 10
    # The silence rule's span (ISSUE_107): a feed that polls successfully and has put NOTHING in
    # the corpus for this long is reported SILENT. A **verdict threshold** by the rule above, so
    # config-only and not a per-call parameter — two operators reading the same report must not
    # see different verdicts. Note what it is not: a window on the health rows, which stay
    # lifetime. It spans only the contribution half, and the report says so.
    silence_days: int = 7


class SourceLatencyReportConfig(BaseModel):
    window: str = '7d'


class SourceQuarantineReportConfig(BaseModel):
    # Wider than the others by default: a quarantine ladder is read over weeks, and a 7-day window
    # would hide exactly the recurrence the report exists to show.
    window: str = '30d'


class BreakingReportConfig(BaseModel):
    window: str = '7d'


class BreakingTimelineReportConfig(BaseModel):
    window: str = '7d'


class PromptDriftReportConfig(BaseModel):
    """Wider than the house default on purpose (ISSUE_110).

    This report's statement is a *comparison between prompt versions*, and 7 days at the current
    cadence often contains exactly one — a comparison whose default window shows nothing to compare
    against is the wrong default. 30 days spans the last three prompt generations.
    """
    window: str = '30d'


class PerfReportConfig(BaseModel):
    window: str = '7d'


class CostReportConfig(BaseModel):
    """The one report whose scope is a *set* of windows rather than one.

    Its statement is the comparison — this week against this month against all-time — so the config
    declares the set. A per-call `window` replaces the set with that single window for that call,
    rather than appending a fourth nobody asked about.
    """
    windows: List[str] = Field(default_factory=lambda: ['7d', '30d', 'all'])
    # How many recent passes the spend prediction averages over.
    recent_passes: int = 20


class ReportsConfig(BaseModel):
    """One config object per report, keyed by the name the catalog and the API use."""
    source_health: SourceHealthReportConfig = Field(default_factory=SourceHealthReportConfig)
    source_latency: SourceLatencyReportConfig = Field(default_factory=SourceLatencyReportConfig)
    source_quarantine: SourceQuarantineReportConfig = Field(
        default_factory=SourceQuarantineReportConfig)
    breaking: BreakingReportConfig = Field(default_factory=BreakingReportConfig)
    breaking_timeline: BreakingTimelineReportConfig = Field(
        default_factory=BreakingTimelineReportConfig)
    prompt_drift: PromptDriftReportConfig = Field(default_factory=PromptDriftReportConfig)
    perf: PerfReportConfig = Field(default_factory=PerfReportConfig)
    cost: CostReportConfig = Field(default_factory=CostReportConfig)
