"""Reading a folder without writing to it (#138) — a fake IMAP server, no network."""
import imaplib

import pytest

from finiexragengine.core.mail import imap_mailbox
from finiexragengine.core.mail.imap_mailbox import ImapMailbox
from finiexragengine.exceptions.ragengine_errors import SourceFetchError


class _FakeImap:
    """Answers only read commands; any write command fails the test."""
    commands: list = []

    def __init__(self, host, port, timeout=None):
        self.uidvalidity = _FakeImap.uidvalidity
        _FakeImap.commands = []

    def login(self, account, password):
        if password != 'right':
            raise imaplib.IMAP4.error('[AUTHENTICATIONFAILED] Invalid credentials (Failure)')
        _FakeImap.commands.append('LOGIN')

    def select(self, mailbox, readonly=False):
        assert readonly, 'the folder must be opened with EXAMINE'
        _FakeImap.commands.append('EXAMINE')
        return 'OK', [b'3']

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command, *args):
        _FakeImap.commands.append(f'UID {command} {args[-1] if args else ""}')
        if command == 'SEARCH':
            return 'OK', [b' '.join(str(uid).encode() for uid in _FakeImap.uids)]
        uid = int(args[0])
        if args[1] == '(RFC822.SIZE)':
            return 'OK', [f'{uid} (UID {uid} RFC822.SIZE {_FakeImap.sizes.get(uid, 100)})'.encode()]
        assert 'PEEK' in args[1], 'bodies are fetched with BODY.PEEK so nothing is marked seen'
        return 'OK', [(f'{uid} (UID {uid} BODY[] {{10}}'.encode(), f'mail-{uid}'.encode()), b')']

    def logout(self):
        _FakeImap.commands.append('LOGOUT')

    def __getattr__(self, name):
        raise AssertionError(f'write or unexpected command {name!r}')


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _FakeImap.uidvalidity = 7
    _FakeImap.uids = [3, 4, 5]
    _FakeImap.sizes = {}
    monkeypatch.setattr(imap_mailbox.imaplib, 'IMAP4_SSL', _FakeImap)


def _mailbox(password: str = 'right') -> ImapMailbox:
    return ImapMailbox('imap.test', 993, 'INBOX', 'bot@test', password, 5.0)


def test_only_mails_above_the_cursor_are_read_and_the_folder_is_examined():
    uidvalidity, fetched = _mailbox().read_new(7, 3, max_mail_bytes=1_000)
    assert uidvalidity == 7 and [mail.uid for mail in fetched] == [4, 5]
    assert 'EXAMINE' in _FakeImap.commands and _FakeImap.commands[-1] == 'LOGOUT'


def test_a_changed_uidvalidity_reads_everything_again():
    _, fetched = _mailbox().read_new(6, 5, max_mail_bytes=1_000)
    assert [mail.uid for mail in fetched] == [3, 4, 5]


def test_an_oversized_mail_is_never_downloaded():
    _FakeImap.sizes = {4: 5_000}
    _, fetched = _mailbox().read_new(7, 3, max_mail_bytes=1_000)
    oversized = next(mail for mail in fetched if mail.uid == 4)
    assert oversized.raw is None and oversized.size_bytes == 5_000


def test_a_refused_login_is_a_durable_failure():
    with pytest.raises(SourceFetchError) as caught:
        _mailbox(password='wrong').read_new(None, 0, max_mail_bytes=1_000)
    assert (caught.value.error_type, caught.value.status) == ('HTTP_ERROR', 401)


def test_an_unreachable_server_is_typed_as_such(monkeypatch):
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError('refused')
    monkeypatch.setattr(imap_mailbox.imaplib, 'IMAP4_SSL', refuse)
    with pytest.raises(SourceFetchError) as caught:
        _mailbox().read_new(None, 0, max_mail_bytes=1_000)
    assert caught.value.error_type == 'UNREACHABLE'
