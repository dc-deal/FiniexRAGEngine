"""Mail intake — what the e-mail sources took in, refused and extracted, per day (#138).

The live status says how a mailbox is doing now; this says how it went: per UTC day of intake and
source, the mails seen, accepted and refused (by reason), the stories their extracts produced and
what the extracts cost. A refused mail is the number to watch — a sender that changed its address or
its signing shows up here as `dmarc` or `sender` refusals rather than as silence.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from finiexragengine.core.mail.mail_store import MailStore


@dataclass
class MailIntakeReport:
    since_label: str
    rows: List[Dict[str, Any]] = field(default_factory=list)
    # True when migration 020 has not run on this database — an honest empty answer, not a crash.
    tables_missing: bool = False


def build_mail_intake_report(database_url: str, since: Optional[datetime], *,
                             since_label: str = '30d') -> MailIntakeReport:
    store = MailStore(database_url)
    report = MailIntakeReport(since_label=since_label)
    if not store.tables_exist():
        report.tables_missing = True
        return report
    report.rows = store.intake_rows(since or datetime(1970, 1, 1).astimezone())
    return report


def format_mail_intake_report(report: MailIntakeReport) -> str:
    if report.tables_missing:
        return f'mail intake · {report.since_label} · no mail tables here (migration 020 not applied)'
    if not report.rows:
        return f'mail intake · {report.since_label} · no mail taken in'
    lines = [f'mail intake · {report.since_label} · {len(report.rows)} source-day(s)',
             f"{'day':10}  {'source':22} {'seen':>5} {'acc':>5} {'rej s/d/z':>10} {'stories':>8} "
             f"{'usd':>8} {'extract s':>10}"]
    for row in report.rows:
        refused = f"{row['rejected_sender']}/{row['rejected_dmarc']}/{row['rejected_size']}"
        lines.append(f"{row['day']:10}  {row['source_id'][:22]:22} {row['seen']:>5} {row['accepted']:>5} "
                     f"{refused:>10} {row['stories']:>8} {row['usd']:>8.4f} {row['extract_seconds']:>10.1f}")
    totals = {key: sum(row[key] for row in report.rows)
              for key in ('seen', 'accepted', 'stories', 'usd')}
    refused_total = sum(row['rejected_sender'] + row['rejected_dmarc'] + row['rejected_size']
                        for row in report.rows)
    lines.append(f"total: {totals['seen']} seen · {totals['accepted']} accepted · {refused_total} refused · "
                 f"{totals['stories']} stories · ${totals['usd']:.4f}")
    lines.append('rej s/d/z = refused for sender / DMARC / size')
    return '\n'.join(lines)
