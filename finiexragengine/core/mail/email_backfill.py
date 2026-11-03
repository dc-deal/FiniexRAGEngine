"""Mails that arrived before the mailbox existed (#138): a folder of `.eml` files into `mail_raw`.

The same parse, the same gate and the same store as live intake — only the provider whose verdict
counts is named by the caller, because these files were received by another mailbox (the operator's
own, from which they were saved). Only raw mails are written; the live source's extract step turns
them into stories at its own pace, so every story has one provenance. Idempotent by Message-ID.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from finiexragengine.core.mail.mail_message import parse_mail
from finiexragengine.core.mail.mail_store import MailStore
from finiexragengine.core.mail.sender_gate import SenderGate

BACKFILL_FOLDER = 'backfill'


@dataclass
class BackfillCensus:
    files: int = 0
    accepted: int = 0
    rejected: Dict[str, int] = field(default_factory=dict)
    already_stored: int = 0
    unreadable: List[str] = field(default_factory=list)

    def line(self, dry_run: bool) -> str:
        refused = sum(self.rejected.values())
        reasons = ', '.join(f'{reason} {count}' for reason, count in sorted(self.rejected.items()))
        return (f"{self.files} files · {self.accepted} accepted · {refused} rejected"
                f"{f' ({reasons})' if refused else ''} · {self.already_stored} already stored"
                f"{f' · {len(self.unreadable)} unreadable' if self.unreadable else ''}"
                f"{' · DRY RUN, nothing written' if dry_run else ''}")


def backfill(directory: Path, source_id: str, gate: SenderGate, store: MailStore, *,
             dry_run: bool) -> BackfillCensus:
    census = BackfillCensus()
    for path in sorted(directory.glob('*.eml')):
        census.files += 1
        try:
            mail = parse_mail(path.read_bytes(), fallback_id=f'<backfill-{path.stem}@{source_id}>')
        except (OSError, ValueError) as exc:
            census.unreadable.append(f'{path.name}: {type(exc).__name__}')
            continue
        verdict = gate.check(mail)
        if verdict.accepted:
            census.accepted += 1
        else:
            census.rejected[verdict.reason or 'unknown'] = census.rejected.get(verdict.reason or 'unknown', 0) + 1
        if not dry_run and not store.store_mail(source_id, BACKFILL_FOLDER, None, None, mail, verdict):
            census.already_stored += 1
    return census
