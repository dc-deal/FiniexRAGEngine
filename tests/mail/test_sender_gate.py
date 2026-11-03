"""The sender gate (#138): an allowlisted address, vouched for by the receiving provider only."""
from datetime import datetime, timezone

from finiexragengine.core.mail.sender_gate import SenderGate
from finiexragengine.types.mail_types import ParsedMail

_SENDER = 'briefing@news.example.com'


def _mail(sender: str = _SENDER, results=None, size: int = 1000) -> ParsedMail:
    return ParsedMail(message_id='<1@x>', sender=sender, subject='s',
                      sent_at=datetime(2026, 11, 3, tzinfo=timezone.utc), text='t', size_bytes=size,
                      authentication_results=results if results is not None else [
                          'mx.provider.test; dkim=pass header.i=@example.com; spf=pass; '
                          'dmarc=pass (p=REJECT) header.from=example.com'])


def _gate() -> SenderGate:
    return SenderGate([_SENDER.upper()], 'mx.provider.test', 50_000)


def test_an_allowlisted_sender_with_the_providers_dmarc_pass_is_accepted():
    # The provider reports the organisational domain for a subdomain sender (relaxed alignment).
    assert _gate().check(_mail()).accepted


def test_an_unknown_sender_is_refused_even_with_a_valid_signature():
    assert _gate().check(_mail(sender='other@example.com')).reason == 'sender'


def test_a_dmarc_failure_or_no_provider_header_is_refused():
    failed = ['mx.provider.test; dmarc=fail header.from=example.com']
    assert _gate().check(_mail(results=failed)).reason == 'dmarc'
    assert _gate().check(_mail(results=[])).reason == 'dmarc'


def test_a_forged_header_below_the_providers_own_is_never_read():
    # The sender wrote a lower header claiming `pass`; the provider's own topmost one says `fail`.
    results = ['mx.provider.test; dmarc=fail header.from=example.com',
               'mx.provider.test; dmarc=pass header.from=example.com']
    assert _gate().check(_mail(results=results)).reason == 'dmarc'


def test_a_header_from_another_authserv_or_another_domain_does_not_count():
    other_server = ['mx.attacker.test; dmarc=pass header.from=example.com']
    other_domain = ['mx.provider.test; dmarc=pass header.from=forwarder.test']
    tld_only = ['mx.provider.test; dmarc=pass header.from=com']
    for results in (other_server, other_domain, tld_only):
        assert _gate().check(_mail(results=results)).reason == 'dmarc'


def test_a_forwarded_copy_fails_by_design():
    forwarded = _mail(sender='operator@mail.test',
                      results=['mx.provider.test; dmarc=pass header.from=mail.test'])
    assert _gate().check(forwarded).reason == 'sender'


def test_an_oversized_mail_is_refused_before_anything_else():
    assert _gate().check(_mail(size=60_000)).reason == 'size'
