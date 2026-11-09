"""One extract batch outside the service (#138 addendum): the oldest pending mails, then a census.

The service in `manual` mode takes mails in and delivers stories but extracts nothing; this is where a
backlog is worked off instead — batch by batch, oldest first, each judged before the next. It writes
extracts exactly as the source would and leaves delivery to the service, so the stories reach the corpus
through the one ingest path. A failed mail is recorded like the source records it and stays pending.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from finiexragengine.core.mail.extract_fidelity import stories_fidelity
from finiexragengine.core.mail.mail_extractor import MailExtractor
from finiexragengine.core.mail.mail_store import MailStore
from finiexragengine.exceptions.ragengine_errors import FiniexRagError


@dataclass
class SenderCensus:
    mails: int = 0
    failed: int = 0
    stories: int = 0
    numbers_found: int = 0
    numbers_total: int = 0


@dataclass
class BatchCensus:
    selected: int = 0
    extracted: int = 0
    failed: int = 0
    stories: int = 0
    usd: float = 0.0
    seconds: float = 0.0
    oldest: Optional[datetime] = None
    newest: Optional[datetime] = None
    backlog_after: int = 0
    by_sender: Dict[str, SenderCensus] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)

    def lines(self, dry_run: bool) -> List[str]:
        span = (f' (oldest {self.oldest:%Y-%m-%d} → {self.newest:%Y-%m-%d})' if self.oldest and self.newest else '')
        if dry_run:
            head = [f'batch: {self.selected} mails{span} would be extracted · DRY RUN, nothing written']
        else:
            head = [f'batch: {self.selected} mails{span} · {self.extracted} extracted · {self.failed} failed · '
                    f'{self.stories} stories · ${self.usd:.4f} · {self.seconds:.0f}s']
        for sender, census in sorted(self.by_sender.items()):
            numbers = f' · numbers {census.numbers_found}/{census.numbers_total} literal' if not dry_run else ''
            failed = f' · {census.failed} failed' if census.failed else ''
            head.append(f'  {sender:46} {census.mails} mails · {census.stories} stories{numbers}{failed}')
        head.extend(f'  failed: {failure}' for failure in self.failures)
        head.append(f'backlog after: {self.backlog_after}')
        return head


def run_batch(store: MailStore, extractor: MailExtractor, source_id: str, limit: int, *,
              dry_run: bool) -> BatchCensus:
    census = BatchCensus()
    pending = store.pending(source_id, extractor.version, limit)
    census.selected = len(pending)
    if pending:
        census.oldest, census.newest = pending[0].sent_at, pending[-1].sent_at
    started = time.monotonic()
    for mail in pending:
        sender = census.by_sender.setdefault(mail.sender, SenderCensus())
        sender.mails += 1
        if dry_run:
            continue
        try:
            outcome = extractor.extract(mail)
        except FiniexRagError as exc:
            reason = f'{type(exc).__name__}: {exc}'[:300]
            store.record_failure(mail.mail_id, extractor.version, model=extractor.model,
                                 prompt_hash=extractor.prompt_hash, error=reason)
            census.failed += 1
            sender.failed += 1
            census.failures.append(f'{mail.message_id} — {reason[:120]}')
            continue
        store.record_extract(mail.mail_id, extractor.version, model=outcome.served_model or extractor.model,
                             prompt_hash=extractor.prompt_hash, stories=outcome.stories,
                             dropped=outcome.dropped, prompt_tokens=outcome.prompt_tokens,
                             completion_tokens=outcome.completion_tokens, usd=outcome.usd,
                             duration_ms=outcome.duration_ms)
        found, total = stories_fidelity([story.model_dump() for story in outcome.stories], mail.text)
        census.extracted += 1
        census.stories += len(outcome.stories)
        census.usd += outcome.usd or 0.0
        sender.stories += len(outcome.stories)
        sender.numbers_found += found
        sender.numbers_total += total
    census.seconds = time.monotonic() - started
    census.backlog_after = store.backlog(source_id, extractor.version)
    return census
