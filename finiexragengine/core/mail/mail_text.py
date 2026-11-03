"""The visible text of a mail, with everything personal taken out (#138).

A newsletter mail is mostly markup, links and tracking; the story text is a fraction of it. What the
store keeps is that fraction, and nothing that identifies the reader: every URL (they are personalised
and carry tracking ids), every e-mail address (the recipient's appears in the footer), and every long
token-like string (unsubscribe and session tokens) is removed before the text leaves this module.
"""
import html
import re
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import List, Optional, Tuple

# A cap on the stored text, far above a newsletter's few thousand words: a runaway mail must not
# become a runaway row or a runaway prompt.
MAX_TEXT_CHARS = 120_000

_URL = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)
_ADDRESS = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
# 32+ characters of base64/JWT alphabet without a space is a token, not a word.
_TOKEN = re.compile(r'\b[A-Za-z0-9_\-]{32,}(?:\.[A-Za-z0-9_\-]{8,}){0,2}=*')


class _VisibleText(HTMLParser):
    """Visible text of an HTML mail, one block per line; style, script and head dropped."""

    _HIDDEN = {'style', 'script', 'head', 'title'}
    _BLOCKS = {'p', 'br', 'div', 'tr', 'h1', 'h2', 'h3', 'h4', 'li', 'td', 'table'}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag in self._HIDDEN:
            self._hidden += 1
        if tag in self._BLOCKS:
            self.parts.append('\n')

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Visible text of an HTML document, whitespace collapsed, blank lines dropped."""
    parser = _VisibleText()
    parser.feed(markup)
    return _collapse(html.unescape(''.join(parser.parts)))


def strip_personal(text: str) -> str:
    """Remove URLs, e-mail addresses and token-like strings; collapse what is left."""
    text = _URL.sub('', text)
    text = _ADDRESS.sub('', text)
    text = _TOKEN.sub('', text)
    return _collapse(text)


def visible_text(message: EmailMessage) -> str:
    """The mail's story text: the HTML part when there is one (the plain part of some senders loses
    umlauts), else the plain part; stripped and capped."""
    part = message.get_body(preferencelist=('html', 'plain'))
    if part is None:
        return ''
    content = part.get_content()
    text = html_to_text(content) if part.get_content_type() == 'text/html' else _collapse(content)
    return strip_personal(text)[:MAX_TEXT_CHARS]


def _collapse(text: str) -> str:
    text = re.sub(r'[ \t ​‌‍﻿]+', ' ', text)
    lines = (line.strip() for line in text.splitlines())
    return '\n'.join(line for line in lines if line)


def shorten_account(account: str) -> str:
    """`bot@example.com` → `bot@…` — enough to tell mailboxes apart on a screen."""
    local, _, domain = account.partition('@')
    return f'{local}@…' if domain else local
