"""The shared credential vocabulary (2026-09-08) — one set of patterns, two publishing surfaces.

`GET /v1/logs/{name}` serves log lines and `GET /v1/configs/{name}` serves configuration documents,
and both can carry a secret that arrived by accident. The cases below are written from the shapes
those surfaces actually produce, because a pattern with no real source is a guess that costs a
false positive.
"""
from finiexragengine.utils.redaction import redact


def test_every_credential_shape_is_masked_and_the_surrounding_text_survives():
    cases = [
        # psycopg echoing the DSN it was handed.
        ('connection to postgresql://finiex:hunter2@db:5432/rag failed', 'hunter2'),
        # An HTTP client traceback carrying the header it sent.
        ('headers={"Authorization": "Bearer abc123DEF456ghi"}', 'abc123DEF456ghi'),
        ('openai.AuthenticationError: key sk-proj-AbCdEf123456 rejected', 'sk-proj-AbCdEf123456'),
        # Telegram puts the bot token in the path, so any URL echo leaks it.
        ('POST https://api.telegram.org/bot8012345678:AAF-xyz_123/sendMessage', 'AAF-xyz_123'),
        # The config surface's own shape: a feed whose key rides in the query string.
        ('https://feeds.example.com/rss?apikey=SEKRET123&format=xml', 'SEKRET123'),
        ('https://example.com/feed?format=xml&access_key=abcdef123456', 'abcdef123456'),
    ]
    for line, secret in cases:
        masked, changed = redact(line)
        assert changed, line
        assert secret not in masked, f'{secret} survived in {masked}'
        assert '«redacted»' in masked


def test_masking_keeps_what_makes_the_line_useful():
    """A masked line still has to answer the question it was read for."""
    masked, _ = redact('connection to postgresql://finiex:hunter2@db:5432/rag failed')
    assert masked.startswith('connection to postgresql://finiex:') and masked.endswith('failed')

    # The parameter NAME survives, so a feed URL still says what kind of feed it is — and the
    # parameters around the secret are untouched.
    masked, _ = redact('https://feeds.example.com/rss?apikey=SEKRET123&format=xml')
    assert masked == 'https://feeds.example.com/rss?apikey=«redacted»&format=xml'


def test_an_ordinary_string_is_returned_unchanged_and_says_so():
    """The boolean is the contract: every caller reports what it altered."""
    for line in ('cannot fetch feed (getaddrinfo failed)',
                 'https://cryptoslate.com/feed/',
                 'sk',                                    # too short to be a key
                 'the bearer of bad news'):               # 'bearer' without a token after it
        masked, changed = redact(line)
        assert masked == line and not changed, line
