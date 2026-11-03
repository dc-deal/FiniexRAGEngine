"""Reading one IMAP folder without ever writing to it (#138).

The folder is opened with `EXAMINE` — read-only by protocol, so not even the `\\Seen` flag moves —
and bodies are fetched with `BODY.PEEK[]`. Nothing is labelled, flagged, moved or deleted: the mailbox
stays a pure archive, and the app password needs no right beyond reading. What has been taken in is
the store's business (the highest UID per folder and UIDVALIDITY), not the mailbox's.

Failures are typed for the quarantine ladder the way a feed's are: a refused login is durable (the
password was revoked, it will not heal by itself), a server that drops the session is transient, and
a network failure carries its duration so the ladder can tell a stall from a refusal.
"""
import imaplib
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from finiexragengine.exceptions.ragengine_errors import SourceFetchError

# One poll takes at most this many new mails; the rest wait for the next pass. A newsletter box
# receives a handful a day — the bound exists for a first poll over a full box.
MAX_MAILS_PER_POLL = 50

_UIDVALIDITY = re.compile(rb'UIDVALIDITY (\d+)')


@dataclass(frozen=True)
class FetchedMail:
    uid: int
    size_bytes: int
    raw: Optional[bytes]          # None when the size alone refuses it — the body is never downloaded
    header_only: Optional[bytes] = None


class ImapMailbox:
    """One IMAP account and folder, read-only."""

    def __init__(self, host: str, port: int, folder: str, account: str, password: str,
                 timeout_seconds: float) -> None:
        self._host = host
        self._port = port
        self._folder = folder
        self._account = account
        self._password = password.replace(' ', '')     # Google shows app passwords in groups of four
        self._timeout = timeout_seconds

    def read_new(self, last_uidvalidity: Optional[int], last_uid: int,
                 max_mail_bytes: int) -> Tuple[int, List[FetchedMail]]:
        """The folder's UIDVALIDITY and the mails above the cursor, oldest first.

        A UIDVALIDITY that differs from the stored one means the server renumbered the folder: the
        cursor no longer means anything, so everything is read again and the store's Message-ID key
        keeps it from being stored twice.
        """
        try:
            client = imaplib.IMAP4_SSL(self._host, self._port, timeout=self._timeout)
        except OSError as exc:
            raise _unreachable(exc) from exc
        try:
            try:
                client.login(self._account, self._password)
            except imaplib.IMAP4.error as exc:
                # The server's words, never the password: imaplib does not echo credentials.
                raise SourceFetchError(f'IMAP login refused: {_reason(exc)}',
                                       error_type='HTTP_ERROR', status=401) from exc
            status, _ = client.select(f'"{self._folder}"', readonly=True)   # EXAMINE
            if status != 'OK':
                raise SourceFetchError(f'IMAP folder {self._folder!r} cannot be opened',
                                       error_type='HTTP_ERROR', status=404)
            uidvalidity = self._uidvalidity(client)
            start_uid = last_uid + 1 if last_uidvalidity == uidvalidity else 1
            status, data = client.uid('SEARCH', None, f'UID {start_uid}:*')
            uids = sorted(int(token) for token in (data[0] or b'').split() if int(token) >= start_uid)
            fetched = [self._fetch(client, uid, max_mail_bytes) for uid in uids[:MAX_MAILS_PER_POLL]]
            return uidvalidity, fetched
        except imaplib.IMAP4.abort as exc:
            raise SourceFetchError(f'IMAP session dropped: {_reason(exc)}',
                                   error_type='HTTP_ERROR', status=503) from exc
        except imaplib.IMAP4.error as exc:
            raise SourceFetchError(f'IMAP refused a command: {_reason(exc)}',
                                   error_type='HTTP_ERROR', status=400) from exc
        except OSError as exc:      # socket.timeout is an OSError
            raise _unreachable(exc) from exc
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    def _uidvalidity(self, client: imaplib.IMAP4_SSL) -> int:
        """The folder's UIDVALIDITY from the EXAMINE answer, or by STATUS when it was not announced."""
        _, values = client.response('UIDVALIDITY')
        for value in values or []:
            if value:
                return int(value)
        _, data = client.status(f'"{self._folder}"', '(UIDVALIDITY)')
        match = _UIDVALIDITY.search(data[0] if data and data[0] else b'')
        return int(match.group(1)) if match else 0

    @staticmethod
    def _fetch(client: imaplib.IMAP4_SSL, uid: int, max_mail_bytes: int) -> FetchedMail:
        status, data = client.uid('FETCH', str(uid), '(RFC822.SIZE)')
        match = re.search(rb'RFC822\.SIZE (\d+)', data[0] if data and data[0] else b'')
        size = int(match.group(1)) if match else 0
        if size > max_mail_bytes:
            status, data = client.uid('FETCH', str(uid), '(BODY.PEEK[HEADER])')
            return FetchedMail(uid=uid, size_bytes=size, raw=None, header_only=_payload(data))
        status, data = client.uid('FETCH', str(uid), '(BODY.PEEK[])')
        raw = _payload(data)
        return FetchedMail(uid=uid, size_bytes=size or len(raw or b''), raw=raw)


def _payload(data: list) -> Optional[bytes]:
    for item in data or []:
        if isinstance(item, tuple) and len(item) > 1:
            return item[1]
    return None


def _reason(exc: BaseException) -> str:
    return (str(exc) or type(exc).__name__)[:200]


def _unreachable(exc: BaseException) -> SourceFetchError:
    # The ingestor measures the fetch's duration itself and reads it against the source's deadline.
    return SourceFetchError(f'IMAP server unreachable ({type(exc).__name__})', error_type='UNREACHABLE')
