"""A mail's visible text (#138): story text kept, everything personal removed."""
from email.message import EmailMessage

from finiexragengine.core.mail.mail_message import mail_id, parse_mail
from finiexragengine.core.mail.mail_text import html_to_text, shorten_account, strip_personal


def test_html_becomes_visible_text_with_umlauts_and_without_style():
    markup = ('<html><head><style>.a{}</style><title>T</title></head><body>'
              '<p>Die Fed hat die Zinsen erh&ouml;ht.</p><div>Gr&uuml;&szlig;e</div></body></html>')
    assert html_to_text(markup) == 'Die Fed hat die Zinsen erhöht.\nGrüße'


def test_urls_addresses_and_tokens_never_survive():
    text = ('Read more https://click.example.com/?jwt=abc · sent to reader@mail.test · '
            'token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.sig12345 · end')
    cleaned = strip_personal(text)
    assert 'http' not in cleaned and '@' not in cleaned and 'eyJ' not in cleaned
    assert cleaned.startswith('Read more') and cleaned.endswith('end')


def test_a_mail_is_parsed_with_its_utc_sending_time_and_a_stable_id():
    message = EmailMessage()
    message['From'] = 'Briefing <Briefing@Example.com>'
    message['Subject'] = 'Morning'
    message['Date'] = 'Mon, 02 Nov 2026 22:09:01 -0600'
    message['Message-ID'] = '<abc@example.com>'
    message.set_content('plain')
    message.add_alternative('<p>Story one.</p>', subtype='html')
    parsed = parse_mail(message.as_bytes(), fallback_id='<fallback@x>')
    assert parsed.sender == 'briefing@example.com'
    assert parsed.sent_at.isoformat() == '2026-11-03T04:09:01+00:00'     # the issue's UTC day
    assert parsed.text == 'Story one.'
    assert mail_id('<ABC@example.com>') == mail_id('<abc@example.com>')


def test_the_account_is_shortened_for_a_screen():
    assert shorten_account('bot@example.com') == 'bot@…'
