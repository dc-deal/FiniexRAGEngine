"""The mail source's record (#138) — needs a reachable Postgres (skipped otherwise)."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from finiexragengine.core.mail.email_backfill import backfill
from finiexragengine.core.mail.mail_message import mail_id
from finiexragengine.core.mail.mail_store import MAX_EXTRACT_ATTEMPTS, MailStore
from finiexragengine.core.mail.sender_gate import SenderGate
from finiexragengine.core.mail.story_articles import story_articles
from finiexragengine.core.rag.pgvector_store import PgVectorStore
from finiexragengine.types.config_types.app_config_types import VectorStoreConfig
from finiexragengine.types.config_types.source_set_types import SourceConfig
from finiexragengine.types.mail_types import GateVerdict, MailStory, ParsedMail

_SENT = datetime(2026, 11, 3, 4, tzinfo=timezone.utc)
_SOURCE = SourceConfig(source_id='newsletter_a', type='email', url='imaps://imap.test/INBOX')


def _mail(n: int, text: str = 'Story text.') -> ParsedMail:
    return ParsedMail(message_id=f'<m{n}@example.com>', sender='briefing@example.com', subject=f's{n}',
                      sent_at=_SENT + timedelta(hours=n), text=text, size_bytes=100)


def _stories(count: int) -> list:
    return [MailStory(headline=f'H{i}', facts='F.', topic='other') for i in range(count)]


@pytest.fixture
def store(clean_db: str) -> MailStore:
    return MailStore(clean_db)


def _extract(store: MailStore, key: str, version: int, count: int) -> None:
    store.record_extract(key, version, model='m', prompt_hash='h', stories=_stories(count), dropped=[],
                         prompt_tokens=1, completion_tokens=1, usd=0.001, duration_ms=5)


def test_a_mail_is_stored_once_and_a_refused_one_keeps_no_text(store):
    assert store.store_mail('newsletter_a', 'INBOX', 9, 1, _mail(1), GateVerdict(True))
    assert not store.store_mail('newsletter_a', 'INBOX', 9, 1, _mail(1), GateVerdict(True))
    assert store.store_mail('newsletter_a', 'INBOX', 9, 2, _mail(2, text='spam'), GateVerdict(False, 'sender'))
    assert store.cursor('newsletter_a', 'INBOX') == (9, 2)
    assert [mail.message_id for mail in store.pending('newsletter_a', 1, 10)] == ['<m1@example.com>']
    rows = store.intake_rows(_SENT - timedelta(days=1))
    assert (rows[0]['seen'], rows[0]['accepted'], rows[0]['rejected_sender']) == (2, 1, 1)


def test_a_failing_mail_is_retried_only_up_to_the_cap(store):
    store.store_mail('newsletter_a', 'INBOX', 9, 1, _mail(1), GateVerdict(True))
    key = mail_id('<m1@example.com>')
    for _ in range(MAX_EXTRACT_ATTEMPTS):
        assert store.backlog('newsletter_a', 1) == 1
        store.record_failure(key, 1, model='m', prompt_hash='h', error='LLMError: down')
    assert store.backlog('newsletter_a', 1) == 0 and store.pending('newsletter_a', 1, 10) == []


def test_a_new_extract_version_retires_the_older_versions_articles_when_delivered(clean_db, store):
    vectors = PgVectorStore(VectorStoreConfig(), clean_db, dimensions=1536, embedding_model='test-embed')
    store.store_mail('newsletter_a', 'INBOX', 9, 1, _mail(1), GateVerdict(True))
    key = mail_id('<m1@example.com>')
    _extract(store, key, 1, 3)
    old = story_articles(_SOURCE, '<m1@example.com>', _SENT, 1, _stories(3), _SENT)
    vectors.upsert(old, [[0.1] * 1536 for _ in old])
    store.confirm_delivered('newsletter_a', [(key, 1)])
    _extract(store, key, 2, 2)
    assert [extract.extract_version for extract in store.undelivered('newsletter_a', 2)] == [2]
    new = story_articles(_SOURCE, '<m1@example.com>', _SENT, 2, _stories(2), _SENT)
    vectors.upsert(new, [[0.1] * 1536 for _ in new])
    assert store.confirm_delivered('newsletter_a', [(key, 2)]) == 3
    assert vectors.existing_ids([a.article_id for a in old + new]) == {a.article_id for a in new}
    assert store.window_counts('newsletter_a', _SENT - timedelta(days=1)).items == 2


def test_a_purge_removes_mail_extracts_and_articles_together_and_a_dry_run_nothing(clean_db, store):
    vectors = PgVectorStore(VectorStoreConfig(), clean_db, dimensions=1536, embedding_model='test-embed')
    store.store_mail('newsletter_a', 'INBOX', 9, 1, _mail(1), GateVerdict(True))
    _extract(store, mail_id('<m1@example.com>'), 1, 2)
    stories = story_articles(_SOURCE, '<m1@example.com>', _SENT, 1, _stories(2), _SENT)
    vectors.upsert(stories, [[0.1] * 1536 for _ in stories])
    counted = store.purge('<m1@example.com>', dry_run=True)
    assert (counted.mails, counted.extracts, counted.articles) == (1, 1, 2)
    assert store.backlog('newsletter_a', 1) == 0 and vectors.existing_ids([s.article_id for s in stories])
    store.purge('<m1@example.com>', dry_run=False)
    assert vectors.existing_ids([s.article_id for s in stories]) == set()
    assert store.purge('<m1@example.com>', dry_run=False).mails == 0


def _eml(directory: Path, n: int, sender: str = 'briefing@example.com') -> None:
    message = EmailMessage()
    message['From'] = sender
    message['Date'] = 'Mon, 02 Nov 2026 22:09:01 -0600'
    message['Message-ID'] = f'<b{n}@example.com>'
    message['Authentication-Results'] = 'provider.test; dmarc=pass header.from=example.com'
    message.add_alternative('<p>Story.</p>', subtype='html')
    (directory / f'{n}.eml').write_bytes(message.as_bytes())


def test_a_backfill_dry_run_writes_nothing_and_a_rerun_adds_nothing(tmp_path, store):
    for n in (1, 2):
        _eml(tmp_path, n)
    _eml(tmp_path, 3, sender='operator@mail.test')          # a forward: refused by design
    gate = SenderGate(['briefing@example.com'], 'provider.test', 2_000_000)
    dry = backfill(tmp_path, 'newsletter_a', gate, store, dry_run=True)
    assert (dry.files, dry.accepted, dry.rejected) == (3, 2, {'sender': 1})
    assert store.pending('newsletter_a', 1, 10) == []
    backfill(tmp_path, 'newsletter_a', gate, store, dry_run=False)
    again = backfill(tmp_path, 'newsletter_a', gate, store, dry_run=False)
    assert again.already_stored == 3
    pending = store.pending('newsletter_a', 1, 10)
    assert len(pending) == 2 and pending[0].sent_at == datetime(2026, 11, 3, 4, 9, 1, tzinfo=timezone.utc)


def test_quality_rows_split_by_format_and_samples_show_text_beside_stories(store):
    store.store_mail('newsletter_a', 'INBOX', 9, 1, _mail(1, text='Die Notenbank hob den Satz auf 4,85 Prozent.'),
                     GateVerdict(True))
    alert = ParsedMail(message_id='<a1@example.com>', sender='alert@example.com', subject='Eil', sent_at=_SENT,
                       text='Kurz.', size_bytes=10)
    store.store_mail('newsletter_a', 'INBOX', 9, 2, alert, GateVerdict(True))
    store.record_extract(mail_id('<m1@example.com>'), 1, model='m', prompt_hash='h',
                         stories=[MailStory(headline='Rate raised', facts='To 4.85 %, not 5.10 %.', topic='other')],
                         dropped=['ad'], prompt_tokens=1, completion_tokens=1, usd=0.01, duration_ms=2000)
    store.record_extract(mail_id('<a1@example.com>'), 1, model='m', prompt_hash='h', stories=[], dropped=[],
                         prompt_tokens=1, completion_tokens=1, usd=0.001, duration_ms=500)
    rows = {row['sender']: row for row in store.quality_rows(_SENT - timedelta(days=1))}
    briefing = rows['briefing@example.com']
    assert (briefing['mails'], briefing['stories'], briefing['numbers_found'], briefing['numbers_total']) == (1, 1, 1, 2)
    assert (rows['alert@example.com']['mails'], rows['alert@example.com']['empty']) == (1, 1)
    sample = store.samples('Briefing@Example.com', 3)
    assert sample[0]['text'].startswith('Die Notenbank') and sample[0]['stories'][0]['headline'] == 'Rate raised'
    assert store.samples('briefing@example.com', 3, version=2) == []
