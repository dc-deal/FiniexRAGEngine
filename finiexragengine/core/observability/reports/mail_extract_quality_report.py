"""Mail extract quality — how well each newsletter format was turned into stories (#138 addendum).

A mail source carries several formats at once — a morning briefing of ten stories, a one-paragraph
breaking alert, a market column — and one extract instruction may serve some of them well and others
badly. This answers per sender (one sender is one format) and extract version: coverage (stories per
1,000 words of source), fidelity (the extract's numbers found literally in the source — a pointer for
the judge, not a verdict: conversions and spelled-out numbers miss too), empties, failures and cost.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from finiexragengine.core.mail.mail_store import MailStore


@dataclass
class MailExtractQualityReport:
    since_label: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    tables_missing: bool = False


def build_mail_extract_quality_report(database_url: str, since: Optional[datetime], *,
                                      since_label: str = '30d') -> MailExtractQualityReport:
    store = MailStore(database_url)
    report = MailExtractQualityReport(since_label=since_label)
    if not store.tables_exist():
        report.tables_missing = True
        return report
    rows = store.quality_rows(since or datetime(1970, 1, 1).astimezone())
    for row in rows:
        row['words_per_mail'] = round(row['words'] / row['mails']) if row['mails'] else 0
        row['stories_per_1k_words'] = round(row['stories'] * 1000 / row['words'], 1) if row['words'] else 0.0
        row['numbers_literal_pct'] = (round(100 * row['numbers_found'] / row['numbers_total'], 1)
                                      if row['numbers_total'] else None)
    report.rows = sorted(rows, key=lambda row: (-row['mails'], row['sender'], row['extract_version']))
    return report


def format_mail_extract_quality_report(report: MailExtractQualityReport) -> str:
    if report.tables_missing:
        return f'mail extract quality · {report.since_label} · no mail tables here (migration 020 not applied)'
    if not report.rows:
        return f'mail extract quality · {report.since_label} · nothing extracted'
    lines = [f'mail extract quality · {report.since_label} · {len(report.rows)} format(s)',
             f"{'sender':46} {'v':>2} {'mails':>5} {'fail':>4} {'empty':>5} {'words/mail':>10} "
             f"{'stories/1k':>10} {'numbers':>11} {'dropped':>7} {'usd':>8}"]
    for row in report.rows:
        numbers = (f"{row['numbers_found']}/{row['numbers_total']}" if row['numbers_total'] else '—')
        lines.append(f"{row['sender'][:46]:46} {row['extract_version']:>2} {row['mails']:>5} {row['failed']:>4} "
                     f"{row['empty']:>5} {row['words_per_mail']:>10,} {row['stories_per_1k_words']:>10} "
                     f"{numbers:>11} {row['dropped']:>7} {row['usd']:>8.4f}")
    lines.append('numbers = the extract\'s multi-digit numbers found literally in the source (both decimal '
                 'notations); a miss is a pointer for the judge — conversions and spelled-out numbers miss too')
    return '\n'.join(lines)
