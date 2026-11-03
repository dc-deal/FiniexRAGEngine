"""Which mails may enter at all (#138): an allowlisted sender, vouched for by the receiving provider.

The `From` header alone proves nothing — anyone can write it. What proves it is the receiving
provider's own verdict: DMARC passed for the domain in `From`, which means the domain's owner signed
or sent the mail. That verdict lives in an `Authentication-Results` header the provider prepends;
headers further down the mail were written by whoever sent it and may say anything, so only the
topmost header carrying the provider's own `authserv-id` is read. A forwarded copy fails here by
design: the forwarder's domain is in `From` now, and a forward cannot prove the original's origin.
"""
import re
from typing import Iterable, List, Optional

from finiexragengine.types.mail_types import GateVerdict, MailRejectReason, ParsedMail

_DMARC = re.compile(r'\bdmarc=(\w+)', re.IGNORECASE)
_HEADER_FROM = re.compile(r'header\.from=([\w.-]+)', re.IGNORECASE)


class SenderGate:
    """Accepts a mail from an allowlisted address whose domain passed DMARC at the receiving provider."""

    def __init__(self, senders: Iterable[str], authserv_id: str, max_mail_bytes: int) -> None:
        self._senders = {sender.strip().lower() for sender in senders if sender.strip()}
        self._authserv_id = authserv_id.strip().lower()
        self._max_mail_bytes = max_mail_bytes

    def check(self, mail: ParsedMail) -> GateVerdict:
        if mail.size_bytes > self._max_mail_bytes:
            return _refused('size')
        if mail.sender not in self._senders:
            return _refused('sender')
        if not self._dmarc_passed(mail.authentication_results, mail.sender.rsplit('@', 1)[-1]):
            return _refused('dmarc')
        return GateVerdict(accepted=True)

    def _dmarc_passed(self, headers: List[str], domain: str) -> bool:
        provider = self._provider_header(headers)
        if provider is None:
            return False
        verdict = _DMARC.search(provider)
        aligned = _HEADER_FROM.search(provider)
        return bool(verdict and verdict.group(1).lower() == 'pass'
                    and aligned and _aligned(domain, aligned.group(1).lower()))

    def _provider_header(self, headers: List[str]) -> Optional[str]:
        """The topmost header the provider itself added; None when there is none."""
        if not self._authserv_id:
            return None
        for header in headers:      # top first, as the mail carries them
            authserv = header.split(';', 1)[0].strip().split()[0].lower() if header.strip() else ''
            if authserv == self._authserv_id:
                return header
        return None


def _refused(reason: MailRejectReason) -> GateVerdict:
    return GateVerdict(accepted=False, reason=reason)


def _aligned(sender_domain: str, verdict_domain: str) -> bool:
    """The DMARC verdict covers the sender's domain: the same domain, or a parent of it.

    Providers report the domain DMARC was evaluated for, which under relaxed alignment is the
    organisational domain — a mail from `x@newsletters.example.com` is reported as
    `header.from=example.com` (measured on a real newsletter, 2026-11-03). A parent must still be a
    domain with a dot, never a bare top-level label.
    """
    if '.' not in verdict_domain:
        return False
    return sender_domain == verdict_domain or sender_domain.endswith('.' + verdict_domain)
