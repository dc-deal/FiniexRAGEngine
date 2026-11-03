"""The mail intake report's text (#138): per source-day, refusals by reason, an honest empty answer."""
from finiexragengine.core.observability.reports.mail_intake_report import (
    MailIntakeReport,
    format_mail_intake_report,
)


def test_rows_and_totals_are_rendered_with_the_refusal_legend():
    report = MailIntakeReport(since_label='30d', rows=[
        {'source_id': 'newsletter_a', 'day': '2026-11-03', 'seen': 3, 'accepted': 2, 'rejected_sender': 1,
         'rejected_dmarc': 0, 'rejected_size': 0, 'stories': 21, 'usd': 0.02, 'extract_seconds': 80.0}])
    text = format_mail_intake_report(report)
    assert '2026-11-03  newsletter_a' in text and '1/0/0' in text
    assert 'total: 3 seen · 2 accepted · 1 refused · 21 stories · $0.0200' in text
    assert 'rej s/d/z = refused for sender / DMARC / size' in text


def test_a_database_without_the_tables_says_so():
    assert 'migration 020 not applied' in format_mail_intake_report(
        MailIntakeReport(since_label='7d', tables_missing=True))
