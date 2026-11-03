"""One mail's bytes → the `ParsedMail` the intake works with (#138)."""
import hashlib
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Optional

from finiexragengine.core.mail.mail_text import visible_text
from finiexragengine.types.mail_types import ParsedMail


def parse_mail(raw: bytes, fallback_id: str, *, with_text: bool = True) -> ParsedMail:
    """Headers and visible text of one RFC 822 message.

    `fallback_id` stands in for a missing Message-ID (rare, but a mail without one must still be
    countable and idempotent). `sent_at` is the mail's own Date in UTC — never the moment of
    reading — because that is when the stories were current; a Date that cannot be read falls back
    to now. `with_text=False` skips the body for a mail that is refused on its size.
    """
    message: EmailMessage = message_from_bytes(raw, policy=policy.default)
    sender = ''
    if message['From'] is not None and message['From'].addresses:
        sender = message['From'].addresses[0].addr_spec.lower()
    return ParsedMail(
        message_id=(str(message['Message-ID'] or '').strip() or fallback_id),
        sender=sender,
        subject=str(message['Subject'] or '')[:500],
        sent_at=_sent_at(message['Date']),
        text=visible_text(message) if with_text else '',
        size_bytes=len(raw),
        authentication_results=[str(value) for value in
                                (message.get_all('Authentication-Results') or [])])


def mail_id(message_id: str) -> str:
    """The store's key for a mail: stable across mailboxes, folders and re-reads."""
    return hashlib.sha256(message_id.strip().lower().encode('utf-8')).hexdigest()[:32]


def _sent_at(value: Optional[object]) -> datetime:
    try:
        parsed = parsedate_to_datetime(str(value)) if value else None
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
