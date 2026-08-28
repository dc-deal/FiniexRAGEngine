"""The report catalog (ISSUE_104) — one place that knows how to build every report.

A report needs two things: a window, and inputs only the *configuration* can answer —
`configured_ids`/`disabled_ids` for source health, the per-feed `timeouts` for latency, each
pipeline's episode `rules` for breaking. Until now every CLI assembled those itself, roughly twenty
lines apiece. Serving the same reports over HTTP would have meant assembling them a second time,
and two assemblies of one thing drift: ISSUE_82 spent weeks with two episode groupings that were
supposed to agree and quietly did not.

So the assembly lives here, once, and both surfaces call it. The CLIs keep their `format_*`
rendering and lose the resolution — which also returns them to what CLAUDE.md says a CLI is,
parameter reception with no logic in it.

**Every entry is a read.** `build_coverage_report` is deliberately absent and must stay absent: it
calls `QueryVectorCache.get_vector`, and a cache miss is a paid embedding call. A GET that converts
into spend is exactly the hole ISSUE_98 closed, and the way to keep it closed is for the catalog to
have no entry through which it could be reached.

Adding a report later is one entry in `_CATALOG`. It is then listed, callable, window-bounded and —
because the router mounts this behind the protected router — authenticated, without touching a
route.
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.core.observability.reports.breaking_report import build_breaking_report
from finiexragengine.core.observability.reports.cost_report import (
    EvalPipelineInfo,
    build_cost_report,
)
from finiexragengine.core.observability.reports.perf_report import build_perf_report
from finiexragengine.core.observability.reports.prompt_drift_report import (
    build_prompt_drift_report,
)
from finiexragengine.core.observability.reports.breaking_timeline_report import (
    build_breaking_timeline_report,
)
from finiexragengine.core.observability.reports.source_health_report import (
    build_source_health_report,
)
from finiexragengine.core.observability.reports.source_latency_report import (
    build_source_latency_report,
)
from finiexragengine.core.observability.reports.source_quarantine_report import (
    build_quarantine_episode,
    build_source_quarantine_report,
)
from finiexragengine.core.pipeline.breaking_episode_rule import groupings_from_configs
from finiexragengine.core.pipeline.detection_preflight import (
    check_detection_reachability,
)
from finiexragengine.core.pipeline.breaking_story_rule import (
    groupings_from_configs as story_groupings_from_configs,
)
from finiexragengine.types.config_types.report_config_types import ReportsConfig
from finiexragengine.types.report_types import (
    AppliedParam,
    ReportListingEntry,
    ReportParams,
    ResolvedReport,
)
from finiexragengine.utils.report_window import parse_since

# One builder per entry, taking the resolved context. The signature is uniform even though the
# builders' are not: each function below owns both the resolution AND the call shape of its report,
# which is why a report with a second positional argument needs no special case anywhere else.
ReportBuilder = Callable[[str, AppConfigManager, ReportParams], Any]


# `reports.<name>` -> the defaults for the parameters this report declares. A callable rather than a
# plain dict so the values are read from the CURRENT config at call time, never frozen at import.
ConfigDefaults = Callable[[ReportsConfig], Dict[str, Any]]


@dataclass(frozen=True)
class ReportSpec:
    """What one report is called, what it accepts, and how to build it.

    `defaults` is where the config layer meets the call: it returns this report's declared defaults,
    and anything the caller supplied replaces them one key at a time. Before ISSUE_104's config
    block these were literals in this file — invisible to an operator and unreachable from
    `user_configs/`.
    """
    build: ReportBuilder
    summary: str
    params: Tuple[str, ...] = ()
    required: Tuple[str, ...] = ()
    defaults: ConfigDefaults = lambda config: {}
    # Optional last word on the resolved set, for a report where one parameter SUPERSEDES another.
    # Only `cost` needs it today, and it needs it for the promise's sake: leaving a superseded
    # default in the answer would show two values as applied when one of them was not.
    finalize: Optional[Callable[[Dict[str, Any], Dict[str, Any]], None]] = None


# --- the resolvers: config -> the inputs a report cannot derive from the store -----------------

def _source_ids(manager: AppConfigManager) -> Tuple[set, set]:
    """(configured, disabled) source ids across every set.

    `configured` is what makes an orphan detectable — a row in the store that no config references
    any more. `disabled` is a config fact the store has no column for: health records what a poll
    did, `enabled` records whether it is polled at all, and without the mark a switched-off feed
    presents its frozen last poll as a current verdict.
    """
    sets = manager.build_source_set_registry().list_sets()
    configured = {source.source_id for source_set in sets for source in source_set.sources}
    disabled = {source.source_id for source_set in sets for source in source_set.sources
                if not source.enabled}
    return configured, disabled


def _feed_timeouts(manager: AppConfigManager) -> Dict[str, int]:
    """Per-feed deadline: its own override if it has one, else its set's default (ISSUE_76).

    The journal records what happened, never what was allowed — so without this map a `p99 2.3s`
    has nothing to be near.
    """
    return {source.source_id: source.timeout_seconds or source_set.fetch_timeout_seconds
            for source_set in manager.build_source_set_registry().list_sets()
            for source in source_set.sources}


def _pipeline_configs(manager: AppConfigManager) -> List[Any]:
    """Every pipeline's config, materialised.

    A list rather than a generator on purpose: two groupings are built from it (episode and story,
    ISSUE_96), and a generator would be empty by the second.
    """
    return [pipeline.get_config() for pipeline in manager.build_pipeline_registry().list_pipelines()]


# --- the entries ------------------------------------------------------------------------------

def _build_source_health(database_url: str, manager: AppConfigManager,
                         params: ReportParams) -> Any:
    configured, disabled = _source_ids(manager)
    # `silence_days` is read from config, not from `params` (ISSUE_107): it is a verdict threshold,
    # and those are config-only by the rule in `report_config_types` — a caller must not be able to
    # make the same feed look delivering or silent.
    # A feed's declared quiet-time allowance is read from the source sets, not from `reports.`:
    # it is a fact about the feed's rhythm, and the live probe (`feed_doctor`) judges staleness
    # against the very same number. Two surfaces, one declaration.
    allowances = {source.source_id: source.expected_max_age_hours
                  for source_set in manager.build_source_set_registry().list_sets()
                  for source in source_set.sources
                  if source.expected_max_age_hours is not None}
    return build_source_health_report(
        database_url, configured, disabled_ids=disabled, allowances=allowances,
        silence_days=manager.get_config().reports.source_health.silence_days)


def _build_perf(database_url: str, manager: AppConfigManager, params: ReportParams) -> Any:
    return build_perf_report(database_url, params.since, since_label=params.window_label or '7d')


def _eval_pipelines(manager: AppConfigManager) -> Dict[str, EvalPipelineInfo]:
    """Eval cadence and symbol count per pipeline, from the EFFECTIVE config.

    The projection has to reflect what actually runs, so a `user_configs/` override — fewer symbols,
    a different cadence — is included, and the registry's own `is_overridden` marks it.
    """
    registry = manager.build_pipeline_registry()
    return {pipeline.get_config().pipeline_id: EvalPipelineInfo(
        interval_seconds=pipeline.get_config().trigger.cadence_seconds,
        symbol_count=len(pipeline.get_config().symbols),
        overridden=registry.is_overridden(pipeline.get_config().pipeline_id))
        for pipeline in registry.list_pipelines()}


def _cost_window_supersedes_the_set(applied: Dict[str, Any], options: Dict[str, Any]) -> None:
    """A single `window` replaces the configured set, so the set is no longer 'applied'."""
    if options.get('window') is not None:
        applied.pop('windows', None)
        options.pop('windows', None)


def _build_cost(database_url: str, manager: AppConfigManager, params: ReportParams) -> Any:
    """Cost's scope is a *set* of windows — the comparison is the report's statement.

    So a per-call `window` REPLACES the set with that one window rather than appending a fourth
    nobody asked about; with no window given, the configured set stands.
    """
    # The raw expression, never `window_label`: the label is the rendering ('all-time') and the
    # expression is the input ('all'). Two vocabularies for one concept, and passing the wrong one
    # through fails only for the values where they differ.
    requested_window = params.options.get('window')
    windows = ([requested_window] if requested_window
               else params.options.get('windows', ['7d', '30d', 'all']))
    return build_cost_report(database_url, eval_pipelines=_eval_pipelines(manager),
                             credit_usd=manager.get_config().cost.account_credit_usd,
                             recent_passes=params.options.get('recent_passes', 20),
                             windows=windows,
                             # When the price table was last held against the vendor's rates —
                             # every USD figure in this report is derived from it.
                             prices_checked=manager.get_config().pricing.checked)


def _build_source_latency(database_url: str, manager: AppConfigManager,
                          params: ReportParams) -> Any:
    return build_source_latency_report(
        database_url, params.since, since_label=params.window_label or '7d',
        timeouts=_feed_timeouts(manager),
        warn_ratio=manager.get_config().diagnostics.timeout_warn_ratio)


def _build_source_quarantine(database_url: str, manager: AppConfigManager,
                             params: ReportParams) -> Any:
    return build_source_quarantine_report(
        database_url, params.source_id, params.since,
        since_label=params.window_label or '30d',
        ladder_reset_hours=manager.get_config().source_health.ladder_reset_hours)


def _build_quarantine_episode(database_url: str, manager: AppConfigManager,
                              params: ReportParams) -> Any:
    return build_quarantine_episode(database_url, params.source_id, params.episode_start)


def _build_breaking(database_url: str, manager: AppConfigManager, params: ReportParams) -> Any:
    configs = _pipeline_configs(manager)
    # The detection thresholds are resolved here, through the registry factory (ISSUE_106): this is
    # the report an operator opens when nothing is flagging, and "the threshold is out of reach for
    # the feeds that are running" is one of the two answers to that question. The factory matters —
    # a per-machine `enabled: false` is exactly what moves these counts.
    reachability = [check_detection_reachability(source_set)
                    for source_set in manager.build_source_set_registry().list_sets()]
    return build_breaking_report(database_url, params.since,
                                 since_label=params.window_label or '7d',
                                 rules=groupings_from_configs(configs),
                                 stories=story_groupings_from_configs(configs),
                                 reachability=reachability)


def _build_breaking_timeline(database_url: str, manager: AppConfigManager,
                             params: ReportParams) -> Any:
    return build_breaking_timeline_report(
        database_url, params.since, since_label=params.window_label or '7d',
        symbol=params.symbol or '',
        rules=groupings_from_configs(_pipeline_configs(manager)))


def _build_prompt_drift(database_url: str, manager: AppConfigManager,
                        params: ReportParams) -> Any:
    """The per-version score distribution (ISSUE_110).

    Two config inputs, and they are read differently on purpose: the **hold gate** travels inside the
    grouping and is applied to the archive at read time, while the **confirm gate** is passed for
    display only — the confirm counts come from each pass's recorded verdict, so a retune since then
    cannot rewrite what happened.
    """
    configs = _pipeline_configs(manager)
    return build_prompt_drift_report(
        database_url, params.since, since_label=params.window_label or '30d',
        rules=groupings_from_configs(configs),
        confirm_thresholds={config.pipeline_id: config.breaking.urgency_threshold
                            for config in configs})


_CATALOG: Dict[str, ReportSpec] = {
    'source_health': ReportSpec(
        build=_build_source_health,
        defaults=lambda config: {'recent_problems': config.source_health.recent_problems},
        params=('recent_problems',),
        summary='Per-feed poll counts, success rate, flag and quarantine state, plus the recent '
                'problem log and any orphaned source ids. Rolling state, so no window.'),
    'source_latency': ReportSpec(
        build=_build_source_latency, params=('window',),
        defaults=lambda config: {'window': config.source_latency.window},
        summary='Per-source fetch latency (p50/p95/p99) and poll-series gaps, measured against the '
                'deadline each feed is actually judged by.'),
    'source_quarantine': ReportSpec(
        build=_build_source_quarantine, params=('window', 'source_id'), required=('source_id',),
        defaults=lambda config: {'window': config.source_quarantine.window},
        summary="One feed's quarantine episodes in the window, priced against its own poll cadence "
                '— which feeds fail repeatedly, and at which rung of the cool-off ladder.'),
    'source_quarantine_episode': ReportSpec(
        build=_build_quarantine_episode, params=('source_id', 'episode_start'),
        required=('source_id', 'episode_start'),
        summary='One quarantine episode with the poll-by-poll run-up that produced it.'),
    'breaking_timeline': ReportSpec(
        build=_build_breaking_timeline, params=('window', 'symbol'),
        defaults=lambda config: {'window': config.breaking_timeline.window},
        summary='The per-pass breaking on/off series behind the episode count, with the flip count '
                'next to it — optionally narrowed to one symbol.'),
    'prompt_drift': ReportSpec(
        build=_build_prompt_drift, params=('window',),
        defaults=lambda config: {'window': config.prompt_drift.window},
        summary='The urgency distribution per prompt version, per pipeline — confirm and hold-band '
                'shares, the hold/break ratio, and how concentrated the confirm band is. Never '
                'pooled across pipelines.'),
    'perf': ReportSpec(
        build=_build_perf, params=('window',),
        defaults=lambda config: {'window': config.perf.window},
        summary='Per-stage and per-call latency over the window — where a pass spends its time, '
                'and which API calls are the slow ones.'),
    'cost': ReportSpec(
        build=_build_cost, params=('window', 'windows', 'recent_passes'),
        defaults=lambda config: {'windows': config.cost.windows,
                                 'recent_passes': config.cost.recent_passes},
        finalize=_cost_window_supersedes_the_set,
        summary='Real spend per window from the billing log, against the configured credit, plus '
                'the cadence-driven projection. A `window` narrows the set to one.'),
    'breaking': ReportSpec(
        build=_build_breaking, params=('window',),
        defaults=lambda config: {'window': config.breaking.window},
        summary='Confirmed breaking episodes, the detection funnel, reaction times and the '
                'episodes-vs-stories measure over the window.'),
}


def list_reports(config: Optional[ReportsConfig] = None) -> List[ReportListingEntry]:
    """Every report this engine can produce, ordered by name.

    `config` supplies the defaults each entry advertises — a listing that showed values other than
    the ones a call would actually use would be worse than showing none.
    """
    reports_config = config if config is not None else ReportsConfig()
    return [ReportListingEntry(name=name, summary=spec.summary, params=list(spec.params),
                               required=list(spec.required),
                               defaults=spec.defaults(reports_config))
            for name, spec in sorted(_CATALOG.items())]


def resolve(name: str, config: ReportsConfig,
            requested: Optional[Dict[str, Any]] = None) -> ResolvedReport:
    """Fold the configured defaults and the call's own values into one resolved set.

    One code path for both surfaces: a CLI flag and an HTTP query parameter are the same override
    through different doors, and both come back with their origin attached so the answer can say
    which value it used. A `window` is resolved to a concrete `since` here, so nothing downstream
    has to know how `7d` is spelled.
    """
    spec = get_spec(name)
    supplied = {key: value for key, value in (requested or {}).items() if value is not None}
    applied: Dict[str, AppliedParam] = {}
    options: Dict[str, Any] = {}

    for key, default in spec.defaults(config).items():
        if key in supplied:
            applied[key] = AppliedParam(value=supplied[key], source='request')
        else:
            applied[key] = AppliedParam(value=default, source='config')
        options[key] = applied[key].value

    # A parameter this report accepts but configures no default for — `cost`'s `window`, which
    # exists only to narrow the configured *set* and would be meaningless as a standing value.
    # Without this the override would be accepted by the route and then silently ignored, which is
    # the failure this whole provenance model exists to make impossible.
    for key in spec.params:
        if key in supplied and key not in applied:
            applied[key] = AppliedParam(value=supplied[key], source='request')
            options[key] = supplied[key]

    params = ReportParams(source_id=supplied.get('source_id'), symbol=supplied.get('symbol'),
                          episode_start=supplied.get('episode_start'), options=options)
    # The selectors have no configured default — they narrow one call or they are absent.
    for key in ('source_id', 'symbol', 'episode_start'):
        if key in supplied:
            applied[key] = AppliedParam(value=supplied[key], source='request')

    if spec.finalize is not None:
        spec.finalize(applied, options)

    window = options.get('window')
    if window is not None:
        params.since, params.window_label = parse_since(window)
    return ResolvedReport(params=params, applied=applied)


def get_spec(name: str) -> ReportSpec:
    """The spec for one report. Raises `KeyError` for an unknown name — the caller decides how that
    reads on its transport (a 404 over HTTP, an `argparse` error on a console)."""
    return _CATALOG[name]


def build_report(name: str, database_url: str, manager: AppConfigManager,
                 params: Optional[ReportParams] = None) -> Any:
    """Build one report. The single entry point both the API and the CLIs use."""
    return get_spec(name).build(database_url, manager, params or ReportParams())


def format_parameter_line(applied: Dict[str, AppliedParam]) -> str:
    """The console's provenance line — which values were used, and where each came from.

    The API echoes this in its `params` block; a terminal reader needs the same thing, because the
    question "why does my output differ from yours" has the same answer on both surfaces. Values
    from the configuration are marked, values from a flag are marked, and a bound that shortened one
    says so.
    """
    if not applied:
        return 'parameters: none'
    parts = []
    for key, param in applied.items():
        origin = 'flag' if param.source == 'request' else 'config'
        parts.append(f'{key}={param.value} ({origin}{", clamped" if param.clamped else ""})')
    return 'parameters: ' + ' · '.join(parts)
