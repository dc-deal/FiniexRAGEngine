"""Prompt drift (ISSUE_110) — the score distribution per prompt version, DB-free.

Drives `_aggregate_drift` directly with synthetic store rows, like the funnel and timeline suites.
The cases are the near-misses this report exists to prevent: a pooled figure that reads flat while
both streams move, a confirm share that reads healthy on one symbol, and a corpus outage that reads
as a calmer prompt.
"""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.prompt_drift_report import (
    _aggregate_drift,
    format_prompt_drift_report,
)
from finiexragengine.core.pipeline.breaking_episode_rule import (
    BreakingEpisodeRule,
    EpisodeGrouping,
)

_T0 = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
_HOLD = {'crypto_sentiment': EpisodeGrouping(BreakingEpisodeRule(exit_threshold=0.7)),
         'forex_macro_sentiment': EpisodeGrouping(BreakingEpisodeRule(exit_threshold=0.7))}
_GATES = {'crypto_sentiment': 0.8, 'forex_macro_sentiment': 0.8}


def _result(symbol: str, urgency: float, *, threshold: float = 0.8, basis: str = 'llm',
            base: str = '', breaking: bool = None) -> dict:
    result = {'symbol': symbol, 'urgency': urgency, 'signal': 'BUY', 'basis': basis,
              'is_breaking': (urgency >= threshold) if breaking is None else breaking}
    if base:
        result['base_currency'] = base
    return result


def _row(pipeline: str, ts: datetime, results: list, *, version: str = '4',
         prompt_hash: str = 'c45e07e1b260', model: str = 'gpt-4o-mini') -> tuple:
    return (pipeline, {'timestamp': ts.isoformat(), 'prompt_version': version,
                       'prompt_id': 'sentiment-crypto', 'prompt_hash': prompt_hash,
                       'metadata': {'model': model}, 'result': results})


def _series(urgencies: list, *, pipeline: str = 'crypto_sentiment', version: str = '4',
            symbol: str = 'XRPUSD', start_minute: int = 0, **kwargs) -> list:
    return [_row(pipeline, _T0 + timedelta(minutes=10 * (start_minute + i)),
                 [_result(symbol, urgency)], version=version, **kwargs)
            for i, urgency in enumerate(urgencies)]


def _version(report, pipeline_id: str, version: str):
    block = next(p for p in report.pipelines if p.pipeline_id == pipeline_id)
    return next(v for v in block.versions if v.prompt_version == version)


# --- the distribution ---------------------------------------------------------------------------

def test_two_versions_are_separate_rows_with_their_own_shares() -> None:
    """The comparison this report exists for: one row per version, chronological."""
    rows = (_series([0.8, 0.8, 0.7, 0.2], version='3')
            + _series([0.8, 0.9, 0.7, 0.3], version='4', start_minute=10))
    report = _aggregate_drift(rows, '30d', _HOLD, _GATES)

    v3, v4 = _version(report, 'crypto_sentiment', '3'), _version(report, 'crypto_sentiment', '4')
    assert [v.prompt_version for v in report.pipelines[0].versions] == ['3', '4']
    assert v3.scored == 4 and v4.scored == 4
    assert v3.confirm_passes == 2 and v4.confirm_passes == 2
    assert v3.confirm_share == 0.5 and v4.confirm_share == 0.5
    assert v3.hold_passes == 1 and v3.hold_share == 0.25
    # The buckets are the observed value set, descending — not a hard-coded lattice.
    assert report.buckets == [0.9, 0.8, 0.7, 0.3, 0.2]
    assert not report.binned
    assert v3.histogram == {'0.80': 2, '0.70': 1, '0.20': 1}


def test_the_hold_break_ratio_separates_a_collapse_from_a_calm_model() -> None:
    """v3's real signature: it kept parking one step below the gate instead of crossing it.

    A bare confirm share cannot tell "the model stopped seeing urgency" from "the model stopped
    crossing the line" — the ratio can, and that is why it is on the row.
    """
    parked = _aggregate_drift(_series([0.8] + [0.7] * 19, version='3'), '30d', _HOLD, _GATES)
    calm = _aggregate_drift(_series([0.8] + [0.2] * 19, version='3'), '30d', _HOLD, _GATES)

    assert _version(parked, 'crypto_sentiment', '3').confirm_share == 0.05
    assert _version(calm, 'crypto_sentiment', '3').confirm_share == 0.05      # identical
    assert _version(parked, 'crypto_sentiment', '3').hold_break_ratio == 19.0  # and yet
    assert _version(calm, 'crypto_sentiment', '3').hold_break_ratio == 0.0


def test_nothing_confirmed_leaves_the_ratio_undefined_rather_than_zero() -> None:
    report = _aggregate_drift(_series([0.2, 0.3], version='4'), '30d', _HOLD, _GATES)
    assert _version(report, 'crypto_sentiment', '4').hold_break_ratio is None
    assert '—' in format_prompt_drift_report(report, width=200)


# --- what must NOT enter the distribution -------------------------------------------------------

def test_a_mechanical_pass_never_enters_the_distribution() -> None:
    """The corpus-outage case: `basis != 'llm'` means the model never saw the pass.

    Folding those in would make a frozen corpus (2026-08-20, 37 hours) read as "the new prompt got
    calmer" — urgency 0.0 rows the LLM never produced, diluting every share.
    """
    rows = (_series([0.8, 0.8], version='4')
            + [_row('crypto_sentiment', _T0 + timedelta(minutes=10 * (2 + i)),
                    [_result('XRPUSD', 0.0, basis='no_data')]) for i in range(8)])
    report = _aggregate_drift(rows, '30d', _HOLD, _GATES)
    version = _version(report, 'crypto_sentiment', '4')

    assert version.scored == 2 and version.mechanical == 8
    assert version.confirm_share == 1.0            # not 0.2 — the eight passes are not evidence
    assert '0.00' not in version.histogram         # and they contribute no 0.0 mass


def test_the_confirm_share_comes_from_the_recorded_verdict_not_a_re_derivation() -> None:
    """A retune must not rewrite what the archive says happened.

    The row below carries urgency 0.7 with `is_breaking: true` — scored under a threshold that has
    since moved. The report counts the verdict, exactly as `BreakingEpisodeRule` does.
    """
    rows = [_row('crypto_sentiment', _T0, [_result('XRPUSD', 0.7, breaking=True)]),
            _row('crypto_sentiment', _T0 + timedelta(minutes=10),
                 [_result('XRPUSD', 0.9, breaking=False)])]
    version = _version(_aggregate_drift(rows, '30d', _HOLD, _GATES), 'crypto_sentiment', '4')

    assert version.confirm_passes == 1
    assert version.unit_confirms == {'XRPUSD': 1}
    # And the 0.9 pass, not breaking and above the hold gate, is a hold-band pass.
    assert version.hold_passes == 1


# --- concentration ------------------------------------------------------------------------------

def test_a_confirm_band_resting_on_one_unit_is_flagged() -> None:
    """Forex v3: 10.78 % reads healthy, and USDCAD supplied 93 % of it."""
    rows = (_series([0.8] * 9, pipeline='forex_macro_sentiment', symbol='USDCAD', version='3')
            + _series([0.2] * 80, pipeline='forex_macro_sentiment', symbol='USDJPY', version='3',
                      start_minute=9))
    version = _version(_aggregate_drift(rows, '30d', _HOLD, _GATES),
                       'forex_macro_sentiment', '3')

    assert version.confirm_units == 1
    assert version.top_unit == 'USDCAD' and version.top_unit_share == 1.0
    assert version.single_unit_confirm_band
    out = format_prompt_drift_report(_aggregate_drift(rows, '30d', _HOLD, _GATES), width=200)
    assert 'rests on a single analysis unit' in out and 'USDCAD' in out


def test_a_fanned_pair_is_one_analysis_unit_not_two() -> None:
    """ISSUE_70: ETHUSD/ETHEUR share one retrieval query, so they are one analysis.

    Counting both legs would inflate `scored`, `confirm_passes` and — the number that matters here —
    the unit count that is supposed to expose concentration.
    """
    grouping = {'crypto_sentiment': EpisodeGrouping(
        BreakingEpisodeRule(exit_threshold=0.7),
        query_map={'ETHUSD': 'Ethereum ETH', 'ETHEUR': 'Ethereum ETH'})}
    rows = [_row('crypto_sentiment', _T0,
                 [_result('ETHUSD', 0.8, base='ETH'), _result('ETHEUR', 0.8, base='ETH')])]
    version = _version(_aggregate_drift(rows, '30d', grouping, _GATES), 'crypto_sentiment', '4')

    assert version.scored == 1 and version.confirm_passes == 1
    assert version.confirm_units == 1 and version.unit_confirms == {'Ethereum ETH': 1}


# --- the pooled trap ----------------------------------------------------------------------------

def test_pooled_reads_flat_while_both_pipelines_move_and_the_report_never_pools() -> None:
    """The v3 → v4 measurement, reproduced: 6.67 % → 6.60 % pooled, both streams rebuilt.

    Anyone measuring only the aggregate would have reported "no effect" twice. So the report must
    show the split — and must have nowhere to put a pooled figure.
    """
    rows = (_series([0.8] + [0.2] * 9, version='3')                                    # crypto 10%
            + _series([0.8] * 9 + [0.2], pipeline='forex_macro_sentiment',
                      symbol='USDCAD', version='3')                                    # forex 90%
            + _series([0.8] * 9 + [0.2], version='4', start_minute=10)                 # crypto 90%
            + _series([0.8] + [0.2] * 9, pipeline='forex_macro_sentiment',
                      symbol='USDCAD', version='4', start_minute=10))                  # forex 10%
    report = _aggregate_drift(rows, '30d', _HOLD, _GATES)

    # Pooled, the two versions are identical: 10 confirms of 20 passes, both times.
    pooled = {version: sum(v.confirm_passes for p in report.pipelines
                           for v in p.versions if v.prompt_version == version)
              for version in ('3', '4')}
    assert pooled['3'] == pooled['4'] == 10
    # Split, every one of the four rows moved by a factor of nine.
    assert _version(report, 'crypto_sentiment', '3').confirm_share == 0.1
    assert _version(report, 'crypto_sentiment', '4').confirm_share == 0.9
    assert _version(report, 'forex_macro_sentiment', '3').confirm_share == 0.9
    assert _version(report, 'forex_macro_sentiment', '4').confirm_share == 0.1
    # The report object holds no cross-pipeline aggregate, and the rendering prints none.
    assert not [name for name in vars(report) if 'total' in name or 'pooled' in name]
    out = format_prompt_drift_report(report, width=200)
    assert 'no pooled figure is emitted' in out


# --- provenance conflicts -----------------------------------------------------------------------

def test_two_hashes_under_one_version_are_flagged_not_split() -> None:
    """An in-place prompt edit: forbidden by the versioning rule, and visible when it happens."""
    rows = (_series([0.8], version='4', prompt_hash='aaaaaaaaaaaa')
            + _series([0.8], version='4', prompt_hash='bbbbbbbbbbbb', start_minute=1))
    report = _aggregate_drift(rows, '30d', _HOLD, _GATES)
    version = _version(report, 'crypto_sentiment', '4')

    assert len(report.pipelines[0].versions) == 1        # one row, not two
    assert version.hash_conflict and version.prompt_hashes == ['aaaaaaaaaaaa', 'bbbbbbbbbbbb']
    assert 'edited in place' in format_prompt_drift_report(report, width=200)


def test_two_models_under_one_version_are_flagged() -> None:
    rows = (_series([0.8], version='4', model='gpt-4o-mini')
            + _series([0.8], version='4', model='gpt-4o', start_minute=1))
    report = _aggregate_drift(rows, '30d', _HOLD, _GATES)

    assert _version(report, 'crypto_sentiment', '4').model_conflict
    assert 'two causes, not one' in format_prompt_drift_report(report, width=200)


def test_an_envelope_without_a_prompt_version_gets_its_own_row() -> None:
    """The archive reaches back before the field existed; silence about it would be a gap."""
    rows = [('crypto_sentiment', {'timestamp': _T0.isoformat(),
                                  'result': [_result('XRPUSD', 0.8)]})]
    report = _aggregate_drift(rows, '30d', _HOLD, _GATES)
    assert _version(report, 'crypto_sentiment', '(none)').scored == 1


# --- the quantisation fallback ------------------------------------------------------------------

def test_more_than_twelve_distinct_values_bins_and_says_so() -> None:
    """The lattice is a measured property of a prompt, not a contract (ISSUE_82).

    A version that emits continuous scores must not render forty columns, and must not be folded
    silently either.
    """
    continuous = [round(0.30 + 0.01 * i, 2) for i in range(20)]
    report = _aggregate_drift(_series(continuous, version='5'), '30d', _HOLD, _GATES)

    assert report.binned
    assert report.buckets == [0.4, 0.3]
    assert _version(report, 'crypto_sentiment', '5').histogram == {'0.40': 10, '0.30': 10}
    assert 'binned to 0.1' in format_prompt_drift_report(report, width=200)


def test_one_runaway_version_bins_the_others_onto_the_same_grid() -> None:
    """The columns must be one shared set, or two versions cannot be compared across."""
    continuous = [round(0.30 + 0.01 * i, 2) for i in range(20)]
    rows = _series([0.8, 0.7], version='4') + _series(continuous, version='5', start_minute=10)
    report = _aggregate_drift(rows, '30d', _HOLD, _GATES)

    assert report.binned
    assert _version(report, 'crypto_sentiment', '4').histogram == {'0.80': 1, '0.70': 1}


# --- degenerate windows -------------------------------------------------------------------------

def test_an_empty_window_renders_the_no_passes_form() -> None:
    report = _aggregate_drift([], '30d', _HOLD, _GATES)
    assert report.pipelines == [] and report.version_count == 0
    assert '(no passes in the window)' in format_prompt_drift_report(report, width=200)


def test_the_header_names_both_gates_and_their_provenance() -> None:
    """The confirm gate is today's config; the hold gate is what was applied to the archive."""
    out = format_prompt_drift_report(
        _aggregate_drift(_series([0.8]), '30d', _HOLD, _GATES), width=200)
    assert 'confirm gate 0.80 (config today)' in out
    assert 'hold band 0.70 (applied)' in out


def test_the_pipeline_header_never_claims_a_model_the_rows_contradict() -> None:
    """A union in the header would read as "this pipeline runs two models".

    One version using a second model for a single pass is a per-row fact, not a pipeline property.
    Naming the count and deferring to the flags is the same discipline as never pooling a share.
    """
    one = _aggregate_drift(_series([0.8], version='4'), '30d', _HOLD, _GATES)
    two = _aggregate_drift(_series([0.8], version='4')
                           + _series([0.8], version='4', model='gpt-4o', start_minute=1),
                           '30d', _HOLD, _GATES)

    def _header(report) -> str:
        return next(line for line in format_prompt_drift_report(report, width=200).splitlines()
                    if line.startswith('crypto_sentiment ·'))

    assert _header(one).endswith('· model gpt-4o-mini')
    assert _header(two).endswith('· 2 models (see flags)')
    assert 'gpt-4o-mini' not in _header(two)


def test_a_unit_is_counted_by_its_episode_key_and_shown_by_its_tickers() -> None:
    """FX episode keys are retrieval queries — correct to count by, unreadable to display.

    Measured on the dev journal before this was fixed: the concentration column read
    `US Dollar Canadian Dollar USD/CAD Bank of Canada BOC 100%`. The key stays in the payload
    (it is what `breaking_episode_id` is built from); the rendering names the ticker.
    """
    query = 'US Dollar Canadian Dollar USD/CAD Bank of Canada BOC'
    grouping = {'forex_macro_sentiment': EpisodeGrouping(
        BreakingEpisodeRule(exit_threshold=0.7), query_map={'USDCAD': query})}
    rows = _series([0.8, 0.8], pipeline='forex_macro_sentiment', symbol='USDCAD', version='3')
    report = _aggregate_drift(rows, '30d', grouping, _GATES)
    version = _version(report, 'forex_macro_sentiment', '3')

    assert version.unit_confirms == {query: 2}          # the key, traceable
    assert version.top_unit == query
    assert version.top_unit_label == 'USDCAD'           # the label, readable
    out = format_prompt_drift_report(report, width=200)
    assert 'USDCAD 100%' in out and query not in out


def test_a_fanned_pair_renders_both_of_its_tickers() -> None:
    grouping = {'crypto_sentiment': EpisodeGrouping(
        BreakingEpisodeRule(exit_threshold=0.7),
        query_map={'ETHUSD': 'Ethereum ETH', 'ETHEUR': 'Ethereum ETH'})}
    rows = [_row('crypto_sentiment', _T0,
                 [_result('ETHUSD', 0.8, base='ETH'), _result('ETHEUR', 0.8, base='ETH')])]
    version = _version(_aggregate_drift(rows, '30d', grouping, _GATES), 'crypto_sentiment', '4')

    assert version.top_unit == 'Ethereum ETH' and version.top_unit_label == 'ETHUSD/ETHEUR'


def test_it_agrees_with_the_timeline_report_on_the_same_rows() -> None:
    """Two aggregations over one query must not drift apart (the ISSUE_82 lesson).

    `scored`, the confirm count and `mechanical` are defined here and in the timeline report in the
    same words — per analysis unit per pass, verdict as recorded, mechanical rows excluded from the
    evidence. So the sum ACROSS VERSIONS has to equal the timeline's totals for the same rows,
    version by version notwithstanding.

    Verified against the dev journal on 2026-08-26 across five (window, pipeline) combinations,
    including an orphan variant pipeline — exact match on all three numbers. This pins it.

    A note on how NOT to check it: comparing one version's confirm share against the timeline's
    all-versions share disagrees by construction, and looks like a defect. That is the same
    split-versus-pooled confusion this report exists to prevent, met while validating it.
    """
    from finiexragengine.core.observability.reports.breaking_timeline_report import (
        _aggregate_timeline,
    )
    rows = (_series([0.8, 0.7, 0.2, 0.9], version='3')
            + _series([0.8, 0.8, 0.3], version='4', start_minute=10)
            + [_row('crypto_sentiment', _T0 + timedelta(minutes=200),
                    [_result('XRPUSD', 0.0, basis='no_data')])]
            + _series([0.8, 0.5], pipeline='forex_macro_sentiment', symbol='USDCAD', version='3'))

    drift = _aggregate_drift(rows, '30d', _HOLD, _GATES)
    timeline = _aggregate_timeline(rows, '30d', '', _HOLD)

    for block in drift.pipelines:
        series = [row for row in timeline.rows if row.pipeline_id == block.pipeline_id]
        assert sum(v.scored for v in block.versions) == sum(r.passes for r in series)
        assert sum(v.confirm_passes for v in block.versions) == sum(
            r.breaking_passes for r in series)
        assert sum(v.mechanical for v in block.versions) == sum(r.mechanical for r in series)
