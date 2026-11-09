"""A separate extract batch (#138 addendum): fidelity, the census per format, failures kept pending."""
from datetime import datetime, timedelta, timezone

from finiexragengine.core.mail.extract_batch import run_batch
from finiexragengine.core.mail.extract_fidelity import numbers_found, stories_fidelity
from finiexragengine.core.mail.mail_extractor import ExtractOutcome
from finiexragengine.exceptions.ragengine_errors import LLMError
from finiexragengine.types.mail_types import MailStory, PendingMail

_T = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_a_number_is_found_in_every_decimal_notation():
    source = 'Umsatz 114,48 Mrd., rund 20.000 Stimmzettel, Grenze von 5.812,50 auf 6943,50 Euro'
    assert numbers_found('$114.48 bn, 20,000 ballots, €5,812.50 → €6,943.50', source) == (4, 4)
    assert numbers_found('up 12% to 99.10', source) == (0, 2)
    assert stories_fidelity([{'headline': 'H 114.48', 'facts': 'rose 77'}], source) == (1, 2)


class _Store:
    def __init__(self, pending):
        self._pending, self.extracts, self.failures = pending, [], []

    def pending(self, source_id, version, limit):
        return self._pending[:limit]

    def record_extract(self, mail_id, version, **fields):
        self.extracts.append(mail_id)

    def record_failure(self, mail_id, version, **fields):
        self.failures.append(mail_id)

    def backlog(self, source_id, version):
        return len(self._pending) - len(self.extracts)


class _Extractor:
    version, model, prompt_hash = 1, 'm', 'h'

    def extract(self, mail):
        if mail.mail_id == 'bad':
            raise LLMError('provider down')
        return ExtractOutcome(stories=[MailStory(headline='Rate 4.85', facts='Raised to 4,85 %.', topic='other')],
                              dropped=['ad'], prompt_tokens=1, completion_tokens=1, duration_ms=1, usd=0.01)


def _mail(key: str, days: int, sender: str) -> PendingMail:
    return PendingMail(key, 'newsletter_a', f'<{key}@x>', _T + timedelta(days=days), 'Leitzins 4,85 Prozent', sender)


def test_a_batch_extracts_its_mails_and_counts_per_format_and_keeps_a_failure_pending():
    store = _Store([_mail('a', 0, 'briefing@x'), _mail('bad', 1, 'alert@x'), _mail('c', 2, 'briefing@x')])
    census = run_batch(store, _Extractor(), 'newsletter_a', 10, dry_run=False)
    assert (census.selected, census.extracted, census.failed, census.stories) == (3, 2, 1, 2)
    assert store.extracts == ['a', 'c'] and store.failures == ['bad']
    assert (census.by_sender['briefing@x'].numbers_found, census.by_sender['briefing@x'].numbers_total) == (4, 4)
    text = '\n'.join(census.lines(dry_run=False))
    assert 'oldest 2026-11-01 → 2026-11-03' in text and 'alert@x' in text and '1 failed' in text


def test_a_dry_run_writes_nothing_and_a_limit_is_kept():
    store = _Store([_mail('a', 0, 'briefing@x'), _mail('b', 1, 'briefing@x')])
    census = run_batch(store, _Extractor(), 'newsletter_a', 1, dry_run=True)
    assert census.selected == 1 and store.extracts == [] and 'DRY RUN' in census.lines(dry_run=True)[0]
