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


def test_the_quality_report_names_each_format_and_explains_the_number_check():
    from finiexragengine.core.observability.reports.mail_extract_quality_report import (
        MailExtractQualityReport,
        format_mail_extract_quality_report,
    )
    report = MailExtractQualityReport(since_label='7d', rows=[
        {'sender': 'briefing@example.com', 'extract_version': 1, 'mails': 10, 'failed': 0, 'empty': 0,
         'words_per_mail': 2180, 'stories_per_1k_words': 4.8, 'numbers_found': 151, 'numbers_total': 156,
         'dropped': 40, 'usd': 0.098},
        {'sender': 'alert@example.com', 'extract_version': 1, 'mails': 9, 'failed': 1, 'empty': 2,
         'words_per_mail': 140, 'stories_per_1k_words': 14.2, 'numbers_found': 0, 'numbers_total': 0,
         'dropped': 0, 'usd': 0.031}])
    text = format_mail_extract_quality_report(report)
    assert 'briefing@example.com' in text and '151/156' in text and '2,180' in text
    assert 'alert@example.com' in text and '—' in text and 'a miss is a pointer for the judge' in text
