"""The MAIL row (#138): one line per e-mail source, shortened account, failure in words."""
from datetime import datetime, timezone

from rich.console import Console

from finiexragengine.core.ui.engine_stats import EngineStats, MailSnapshot
from finiexragengine.core.ui.live_display import LiveDisplay

_NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def _render(stats: EngineStats) -> str:
    console = Console(record=True, width=160, height=40)
    console.print(LiveDisplay(stats)._stage_rows(_NOW))
    return console.export_text()


def test_a_mail_source_gets_its_row_and_an_engine_without_one_none():
    assert 'MAIL' not in _render(EngineStats(source_set_ids=['a'], pipeline_ids=['p']))
    stats = EngineStats(source_set_ids=['a'], pipeline_ids=['p'], mail_source_ids=['newsletter_a'])
    stats.set_mail('newsletter_a', MailSnapshot(last=_NOW, account='bot@…', state='ok', today_mails=3,
                                                today_items=34, week_mails=14, week_items=151))
    text = _render(stats)
    assert 'MAIL' in text and 'newsletter_a' in text
    assert 'bot@… · today 3 mails → 34 items · week 14 → 151 · ok' in text


def test_a_failing_mailbox_says_why_and_since_when():
    stats = EngineStats(mail_source_ids=['newsletter_a'])
    stats.set_mail('newsletter_a', MailSnapshot(last=_NOW, state='failing', failure='IMAP login refused',
                                                failing_since=datetime(2026, 11, 3, 11, 59, tzinfo=timezone.utc),
                                                week_rejected=1, backlog=2))
    text = _render(stats)
    assert 'backlog 2 · 1 rejected · ⚠ IMAP login refused since 11:59' in text
    stats.set_mail('unknown_source', MailSnapshot(last=_NOW))          # never registered: ignored
    assert 'unknown_source' not in _render(stats)
