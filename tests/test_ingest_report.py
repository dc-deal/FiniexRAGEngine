"""Ingest-pass report — every declared source gets a line, whatever became of it."""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.observability.reports.ingest_report import (
    build_ingest_report,
    format_ingest_report,
)
from finiexragengine.types.config_types.source_set_types import SourceConfig, SourceSetConfig
from finiexragengine.types.ingest_types import IngestResult, SourceIngest, SourcePoll


def _source_set() -> SourceSetConfig:
    """Four feeds covering every fate a source can meet in one pass."""
    return SourceSetConfig(source_set_id='forex_news', sources=[
        SourceConfig(source_id='fxstreet', url='https://fx.test/rss', enabled=False,
                     comment='Cloudflare-walled from this egress IP'),
        SourceConfig(source_id='forexlive', url='https://fl.test/rss'),
        SourceConfig(source_id='cnbc_forex', url='https://cnbc.test/rss'),
        SourceConfig(source_id='boe_news', url='https://boe.test/rss'),
    ])


def test_every_declared_source_gets_a_row_in_config_order():
    # The regression this report exists for: a quarantined feed used to be collected into its own
    # list and then dropped by the CLI, which printed only `per_source` + `failed_sources`. A
    # permanent HTTP 403 therefore looked exactly like a clean run. Rendering against the declared
    # catalogue makes omission structurally impossible — a disabled feed included.
    until = datetime.now(timezone.utc) + timedelta(hours=3)
    result = IngestResult(fetched=25, embedded=0, stored=0, polls=[
        SourcePoll('cnbc_forex', 'quarantined', until=until, detail='cool-off'),
        SourcePoll('forexlive', 'ok', ingest=SourceIngest(fetched=25, embedded=0, stored=0)),
        SourcePoll('boe_news', 'failed', detail='returned HTTP 500'),
    ])
    report = build_ingest_report('forex_news', result, _source_set())

    assert [row.source_id for row in report.rows] == \
        ['fxstreet', 'forexlive', 'cnbc_forex', 'boe_news']     # catalogue order, not poll order
    assert [row.status for row in report.rows] == \
        ['disabled', 'ok', 'QUARANTINED', 'FAILED']
    assert report.declared == 4
    assert report.polled == 1        # only forexlive actually reached its feed


def test_disabled_source_shows_its_comment_as_the_reason():
    # A disabled feed is never built, so the ingestor never sees it — the row comes from the
    # catalogue, and `comment` is the field carrying *why* it is off.
    result = IngestResult(polls=[SourcePoll('forexlive', 'ok', ingest=SourceIngest(fetched=1))])
    report = build_ingest_report('forex_news', result, _source_set())
    row = next(r for r in report.rows if r.source_id == 'fxstreet')

    assert row.status == 'disabled'
    assert row.detail == 'Cloudflare-walled from this egress IP'
    assert row.ingest is None        # no counters — it was never polled


def test_source_the_pass_never_reached_is_not_reported_as_healthy():
    # A mid-pass budget suspend (ISSUE_47) breaks the loop, so every later source gets no poll at
    # all. Silence must not read as success: they are labelled, not omitted and not shown as ok.
    result = IngestResult(fetched=25, suspended=True, polls=[
        SourcePoll('forexlive', 'suspended', ingest=SourceIngest(fetched=25),
                   detail='paid work suspended'),
    ])
    report = build_ingest_report('forex_news', result, _source_set())
    statuses = {row.source_id: row.status for row in report.rows}

    assert statuses['forexlive'] == 'SUSPENDED'
    assert statuses['cnbc_forex'] == 'not polled'    # after the break — never considered
    assert statuses['boe_news'] == 'not polled'


def test_format_shows_counts_the_window_line_and_the_cool_off():
    until = datetime.now(timezone.utc) + timedelta(hours=3)
    result = IngestResult(fetched=25, embedded=0, stored=0, polls=[
        SourcePoll('cnbc_forex', 'quarantined', until=until, detail='cool-off'),
        SourcePoll('forexlive', 'ok', ingest=SourceIngest(fetched=25, embedded=0, stored=0)),
        SourcePoll('boe_news', 'failed', detail='returned HTTP 500'),
    ])
    rendered = format_ingest_report(build_ingest_report('forex_news', result, _source_set()))

    # The window line answers the question the old output could not: how many feeds actually ran.
    assert 'sources: 4 declared · 1 polled' in rendered
    assert '1 failed' in rendered and '1 quarantined' in rendered and '1 disabled' in rendered
    assert 'QUARANTINED' in rendered and '3h left' in rendered   # the skip says how long it lasts
    for source_id in ('fxstreet', 'forexlive', 'cnbc_forex', 'boe_news'):
        assert source_id in rendered


def test_empty_pass_still_lists_every_source():
    # Nothing ran at all (e.g. the pass raised before polling) — the table must not collapse to
    # nothing and imply there are no feeds.
    report = build_ingest_report('forex_news', IngestResult(), _source_set())
    rendered = format_ingest_report(report)

    assert report.polled == 0
    assert [row.status for row in report.rows] == \
        ['disabled', 'not polled', 'not polled', 'not polled']
    assert 'forexlive' in rendered
