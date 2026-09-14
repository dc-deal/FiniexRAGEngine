"""The report catalog (ISSUE_104) — needs a reachable Postgres (skipped otherwise).

The catalog's promise is that a report is buildable *and* serializable from its name alone, with the
config resolution done once rather than at each call site. The central test is therefore a loop over
the catalog rather than a case per report: a report added later is covered the moment it is added,
which is the only way a "just add an entry" design stays true.
"""
from datetime import datetime, timedelta, timezone

import pytest

from finiexragengine.configuration.app_config_manager import AppConfigManager
from finiexragengine.types.config_types.report_config_types import ReportsConfig
from finiexragengine.core.observability.reports import report_catalog
from finiexragengine.types.report_types import ReportParams
from finiexragengine.utils.dataclass_json import to_jsonable

_SINCE = datetime.now(timezone.utc) - timedelta(days=7)


def _params_for(spec: report_catalog.ReportSpec) -> ReportParams:
    """Everything this spec declares it needs — a stand-in value for each required parameter."""
    return ReportParams(
        since=_SINCE if 'window' in spec.params else None,
        window_label='7d' if 'window' in spec.params else None,
        source_id='theblock' if 'source_id' in spec.params else None,
        episode_start=_SINCE if 'episode_start' in spec.params else None,
        symbol='BTCUSD' if 'symbol' in spec.params else None)


def test_every_entry_builds_and_serializes(clean_db: str) -> None:
    """The loop that makes a new catalog entry self-testing.

    Serialization is asserted alongside building on purpose: a report that builds but carries an
    engine object would pass a build-only test and fail on the wire.
    """
    manager = AppConfigManager()

    for entry in report_catalog.list_reports():
        spec = report_catalog.get_spec(entry.name)
        built = report_catalog.build_report(entry.name, clean_db, manager, _params_for(spec))
        # `source_quarantine_episode` legitimately answers None when no episode matches; every
        # other report answers with a shape.
        if built is not None:
            to_jsonable(built)


def test_the_listing_declares_what_each_report_accepts() -> None:
    entries = {entry.name: entry for entry in report_catalog.list_reports()}

    assert 'source_health' in entries
    assert 'window' not in entries['source_health'].params   # rolling state, no window
    assert entries['source_quarantine'].required == ['source_id']
    # The advertised default is the CONFIGURED one, so the listing and a call agree.
    assert entries['breaking'].defaults == {'window': '7d'}
    assert all(entry.summary for entry in entries.values())   # a name alone is not a catalog


def test_required_parameters_are_a_subset_of_accepted_ones() -> None:
    """Otherwise a caller could satisfy `required` with a parameter the builder never reads."""
    for entry in report_catalog.list_reports():
        assert set(entry.required) <= set(entry.params), entry.name


def test_the_catalog_has_no_entry_that_can_spend() -> None:
    """`build_coverage_report` embeds on a cache miss, i.e. it makes a paid call (ISSUE_19).

    A GET that converts into spend is the hole ISSUE_98 closed. Keeping coverage out of the catalog
    is what keeps it closed, so the absence is asserted rather than assumed.

    `floor_profile` is here for the same reason and had to be named explicitly (ISSUE_106): it
    embeds an uncached query too. **Spend is the criterion, not weight** — `detection_sweep` was
    excluded for years for being a heavy self-join and was admitted once that was noticed, so the
    two properties are pinned apart here rather than left to the next reader's judgement.
    """
    names = {entry.name for entry in report_catalog.list_reports()}

    assert 'coverage' not in names
    assert not any('coverage' in name for name in names)
    assert 'floor_profile' not in names
    # The other half of the same rule: a read is admitted however heavy it is.
    assert 'detection_sweep' in names


def test_the_sweep_answers_for_every_set_and_narrows_to_one(clean_db: str) -> None:
    """`source_set_id` narrows an answer; it never selects a different report (CLAUDE.md).

    The console has always swept every configured set by default, so the route does too — a report
    that answered for one set under the same name would be a second program wearing the first
    one's.
    """
    manager = AppConfigManager()
    configured = len(manager.build_source_set_registry().list_sets())

    every = report_catalog.build_report('detection_sweep', clean_db, manager, ReportParams())
    narrowed = report_catalog.build_report(
        'detection_sweep', clean_db, manager, ReportParams(source_set_id='crypto_news'))

    assert len(every) == configured
    assert [report.source_set_id for report in narrowed] == ['crypto_news']


def test_an_unknown_name_raises_key_error_for_the_caller_to_translate() -> None:
    """The catalog does not know about HTTP; the router turns this into a 404."""
    with pytest.raises(KeyError):
        report_catalog.get_spec('no_such_report')


# --- the config/call model (ISSUE_104) --------------------------------------------------------

def test_a_configured_default_applies_when_the_call_says_nothing() -> None:
    resolved = report_catalog.resolve('breaking', ReportsConfig(), {})

    assert resolved.applied['window'].value == '7d'
    assert resolved.applied['window'].source == 'config'
    assert resolved.params.since is not None          # resolved to a concrete bound


def test_a_call_overrides_the_configured_default_and_the_origin_says_so() -> None:
    """The provenance is the point: two answers that differ must explain why."""
    resolved = report_catalog.resolve('breaking', ReportsConfig(), {'window': '14d'})

    assert resolved.applied['window'].value == '14d'
    assert resolved.applied['window'].source == 'request'


def test_the_configured_value_is_read_at_call_time_not_frozen_at_import() -> None:
    """`user_configs/` overrides would be dead otherwise — the defect this model removes."""
    overridden = ReportsConfig()
    overridden.breaking.window = '21d'

    resolved = report_catalog.resolve('breaking', overridden, {})

    assert resolved.applied['window'].value == '21d'


def test_a_parameter_with_no_configured_default_is_still_honoured() -> None:
    """`cost` configures a window *set*; a single `window` exists only to narrow it.

    Without this, the route would accept `?window=` and then silently ignore it — the precise
    failure the provenance model exists to make impossible.
    """
    resolved = report_catalog.resolve('cost', ReportsConfig(), {'window': '14d'})

    assert resolved.applied['window'].source == 'request'
    assert resolved.params.options['window'] == '14d'


def test_a_single_window_supersedes_the_configured_set_visibly() -> None:
    """The superseded default must not be reported as applied — that would show two values in
    force when only one was."""
    resolved = report_catalog.resolve('cost', ReportsConfig(), {'window': '14d'})

    assert 'windows' not in resolved.applied
    assert 'windows' not in resolved.params.options

    untouched = report_catalog.resolve('cost', ReportsConfig(), {})
    assert untouched.applied['windows'].value == ['7d', '30d', 'all']


def test_the_console_parameter_line_names_every_value_and_its_origin() -> None:
    """The operator at a terminal needs the same answer the API gives in `params`."""
    resolved = report_catalog.resolve('breaking_timeline', ReportsConfig(),
                                      {'symbol': 'XRPUSD'})

    line = report_catalog.format_parameter_line(resolved.applied)

    assert 'window=7d (config)' in line
    assert 'symbol=XRPUSD (flag)' in line
