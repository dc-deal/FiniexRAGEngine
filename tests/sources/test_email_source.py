"""The e-mail source (#138): raw first, an LLM outage never stops intake, unstored stories return."""
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Dict, List, Optional, Tuple

import pytest

from finiexragengine.core.mail.imap_mailbox import FetchedMail
from finiexragengine.core.mail.mail_extractor import ExtractOutcome
from finiexragengine.core.mail.mail_message import mail_id
from finiexragengine.core.mail.sender_gate import SenderGate
from finiexragengine.core.sources.email_source import EmailSource, parse_mailbox_url
from finiexragengine.exceptions.ragengine_errors import LLMError, SourceFetchError
from finiexragengine.types.config_types.app_config_types import MailboxConfig
from finiexragengine.types.config_types.source_set_types import SourceConfig
from finiexragengine.types.mail_types import (
    DeliverableExtract,
    MailStory,
    MailWindowCounts,
    PendingMail,
)

_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
_SENDER = 'briefing@example.com'


def _raw(uid: int, sender: str = _SENDER) -> bytes:
    message = EmailMessage()
    message['From'] = sender
    message['Date'] = 'Tue, 03 Nov 2026 04:00:00 +0000'
    message['Message-ID'] = f'<m{uid}@example.com>'
    message['Authentication-Results'] = 'mx.test; dmarc=pass header.from=example.com'
    message.add_alternative(f'<p>Story text {uid}.</p>', subtype='html')
    return message.as_bytes()


class _Mailbox:
    def __init__(self, uids: List[int], fail: Optional[SourceFetchError] = None) -> None:
        self.uids, self.fail, self.calls = uids, fail, []

    def read_new(self, uidvalidity, last_uid, max_mail_bytes):
        self.calls.append((uidvalidity, last_uid))
        if self.fail:
            raise self.fail
        return 9, [FetchedMail(uid=uid, size_bytes=200, raw=_raw(uid)) for uid in self.uids if uid > last_uid]


class _Store:
    """The store's contract in memory, recording the order of writes."""

    def __init__(self) -> None:
        self.events: List[str] = []
        self.mails: Dict[str, Tuple] = {}
        self.extracts: Dict[Tuple[str, int], dict] = {}
        self.confirmed: List[Tuple[str, int]] = []

    def cursor(self, source_id, folder):
        uids = [row[1] for row in self.mails.values()]
        return (9, max(uids)) if uids else (None, 0)

    def store_mail(self, source_id, folder, uidvalidity, uid, mail, verdict):
        key = mail_id(mail.message_id)
        if key in self.mails:
            return False
        self.events.append(f'store {mail.message_id} {verdict.accepted}')
        self.mails[key] = (mail, uid, verdict)
        return True

    def pending(self, source_id, version, limit):
        return [PendingMail(key, source_id, mail.message_id, mail.sent_at, mail.text)
                for key, (mail, uid, verdict) in self.mails.items()
                if verdict.accepted and not (self.extracts.get((key, version)) or {}).get('stories')][:limit]

    def record_extract(self, key, version, **fields):
        self.events.append(f'extract {key[:6]}')
        self.extracts[(key, version)] = dict(fields, delivered=False)

    def record_failure(self, key, version, **fields):
        self.events.append(f'failure {key[:6]}')
        self.extracts[(key, version)] = {'stories': None, 'error': fields['error']}

    def undelivered(self, source_id, version):
        return [DeliverableExtract(key, self.mails[key][0].message_id, self.mails[key][0].sent_at, version,
                                   list(row['stories']))
                for (key, v), row in self.extracts.items()
                if v == version and row.get('stories') and not row.get('delivered')]

    def confirm_delivered(self, source_id, keys):
        for key in keys:
            self.extracts[key]['delivered'] = True
            self.confirmed.append(key)
        return 0

    def window_counts(self, source_id, since):
        return MailWindowCounts(mails=len(self.mails))

    def backlog(self, source_id, version):
        return len(self.pending(source_id, version, 100))


class _Extractor:
    version, model, prompt_hash = 1, 'm', 'h'

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def extract(self, mail):
        if self.fail:
            raise LLMError('provider unavailable')
        return ExtractOutcome(stories=[MailStory(headline=f"H {mail.message_id.strip('<>').split('@')[0]}", facts='F.', topic='other')],
                              dropped=[], prompt_tokens=10, completion_tokens=5, duration_ms=3, usd=0.01)


def _source(mailbox, store, extractor) -> EmailSource:
    config = SourceConfig(source_id='newsletter_a', type='email', url='imaps://imap.test:993/INBOX')
    mailbox_config = MailboxConfig(account='bot@test', app_password='x', authserv_id='mx.test',
                                   senders=[_SENDER])
    return EmailSource(config, mailbox_config, mailbox, 'INBOX', store,
                       SenderGate([_SENDER], 'mx.test', 2_000_000), extractor, clock=lambda: _NOW)


def test_a_mail_is_stored_raw_before_any_extract_and_its_stories_are_delivered():
    store = _Store()
    articles = _source(_Mailbox([1, 2]), store, _Extractor()).fetch()
    assert store.events[:2] == ['store <m1@example.com> True', 'store <m2@example.com> True']
    assert [event.split()[0] for event in store.events[2:]] == ['extract', 'extract']
    assert [article.title for article in articles] == ['H m1', 'H m2']
    assert all(article.published_at == datetime(2026, 11, 3, 4, tzinfo=timezone.utc) for article in articles)


def test_an_llm_outage_keeps_intake_running_and_leaves_a_backlog():
    store = _Store()
    source = _source(_Mailbox([1]), store, _Extractor(fail=True))
    assert source.fetch() == []                       # no raise: the outage is not the mailbox's fault
    assert 'store <m1@example.com> True' in store.events and any(e.startswith('failure') for e in store.events)
    assert source.mail_status().state == 'ok' and source.mail_status().extract_backlog == 1


def test_stories_are_confirmed_on_the_next_pass_and_handed_out_again_after_a_reset():
    store = _Store()
    source = _source(_Mailbox([1]), store, _Extractor())
    first = source.fetch()
    source.reset_conditional_get()                    # the pass did not store them
    again = source.fetch()
    assert [a.article_id for a in again] == [a.article_id for a in first] and store.confirmed == []
    source.fetch()                                     # the previous pass completed
    assert len(store.confirmed) == 1


def test_a_mailbox_failure_raises_typed_and_reads_as_failing():
    failure = SourceFetchError('IMAP login refused', error_type='HTTP_ERROR', status=401)
    source = _source(_Mailbox([], fail=failure), _Store(), _Extractor())
    with pytest.raises(SourceFetchError):
        source.fetch()
    status = source.mail_status()
    assert status.state == 'failing' and 'login refused' in status.failure and status.failing_since == _NOW


def test_a_rejected_sender_is_stored_as_refused_and_never_extracted(caplog):
    class _Spoofed(_Mailbox):
        def read_new(self, uidvalidity, last_uid, max_mail_bytes):
            return 9, [FetchedMail(uid=1, size_bytes=200, raw=_raw(1, sender='someone@else.test'))]
    store = _Store()
    with caplog.at_level(logging.INFO):
        assert _source(_Spoofed([1]), store, _Extractor()).fetch() == []
    assert store.events == ['store <m1@example.com> False']
    assert '1 rejected (sender 1)' in caplog.text


def test_the_mailbox_url_is_imaps_only():
    assert parse_mailbox_url('imaps://imap.test/Newsletters') == ('imap.test', 993, 'Newsletters')
    with pytest.raises(ValueError):
        parse_mailbox_url('imap://imap.test/INBOX')


def test_in_manual_mode_the_service_extracts_nothing_but_delivers_what_a_separate_run_extracted():
    store = _Store()
    source = _source(_Mailbox([1]), store, _Extractor())
    source._mailbox_config = source._mailbox_config.model_copy(update={
        'extract': source._mailbox_config.extract.model_copy(update={'mode': 'manual'})})
    assert source.fetch() == [] and not any(e.startswith('extract') for e in store.events)
    assert source.mail_status().extract_mode == 'manual' and source.mail_status().extract_backlog == 1
    key = next(iter(store.mails))
    store.record_extract(key, 1, stories=[MailStory(headline='H', facts='F.', topic='other')])   # the separate run
    assert [article.title for article in source.fetch()] == ['H']
