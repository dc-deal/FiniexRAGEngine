"""Feed doctor (ISSUE_11) — pull a feed's raw output and diagnose why it fails to parse.

The operator's "get to the error source" tool: it fetches the raw bytes *and* runs the same
feedparser path the ingest worker uses, then classifies the outcome with the exact taxonomy
source-health records — so a red row in the Sources report has a one-command explanation
(e.g. cryptoslate's `not well-formed` is really an HTTP 429 error body, not a broken feed).

Network-touching by design (that is the diagnosis); the pure classifier `classify_feed` is
tested without a network.
"""
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

import feedparser

_UA = 'Mozilla/5.0 (FiniexRAGEngine feed-doctor)'


@dataclass
class FeedDiagnosis:
    source_id: str
    url: str
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    body_bytes: int = 0
    head: str = ''
    bozo: bool = False
    bozo_exception: Optional[str] = None
    entries: int = 0
    transport_error: Optional[str] = None
    verdict: str = 'OK'            # OK / RATE_LIMITED / HTTP_ERROR / PARSE_ERROR / UNREACHABLE
    suspicious: List[str] = field(default_factory=list)


def classify_feed(http_status: Optional[int], transport_error: Optional[str],
                  bozo: bool, entries: int) -> str:
    """The taxonomy verdict — mirrors RssSource so the doctor explains what the worker recorded."""
    if http_status is not None and http_status >= 400:
        return 'RATE_LIMITED' if http_status == 429 else 'HTTP_ERROR'
    if transport_error is not None:
        return 'UNREACHABLE'
    if bozo and not entries:
        return 'PARSE_ERROR'
    return 'OK'


def _scan_suspicious(raw: bytes) -> List[str]:
    """Locate the kind of token that trips XML parsing — invalid control chars or a bare `&`."""
    findings: List[str] = []
    for index, byte in enumerate(raw):
        if byte < 0x20 and byte not in (0x09, 0x0a, 0x0d):
            findings.append(f'control byte 0x{byte:02x} at offset {index}')
            break
    text = raw.decode('utf-8', 'replace')
    match = re.search(r'&(?!#?\w+;)', text)
    if match:
        findings.append(f'bare & at offset {match.start()}: '
                        f'{text[match.start():match.start() + 30]!r}')
    return findings


def diagnose_feed(source_id: str, url: str, *, timeout: int = 20) -> FeedDiagnosis:
    """Raw GET + feedparser parse + classification for one feed."""
    diag = FeedDiagnosis(source_id=source_id, url=url)
    raw = b''
    # 1. Raw fetch — this is where the true HTTP status (e.g. 429) is visible before feedparser
    #    ever tries to treat the body as XML.
    try:
        request = urllib.request.Request(url, headers={'User-Agent': _UA})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            diag.http_status = response.status
            diag.content_type = response.headers.get('Content-Type')
            raw = response.read()
    except urllib.error.HTTPError as exc:
        diag.http_status = exc.code
        diag.content_type = exc.headers.get('Content-Type') if exc.headers else None
        raw = exc.read() or b''
    except (urllib.error.URLError, OSError) as exc:
        diag.transport_error = f'{type(exc).__name__}: {exc}'
    diag.body_bytes = len(raw)
    diag.head = raw[:300].decode('utf-8', 'replace')

    # 2. Parse through feedparser (the worker's path) for bozo / entries.
    parsed = feedparser.parse(url)
    diag.bozo = bool(getattr(parsed, 'bozo', 0))
    exc = getattr(parsed, 'bozo_exception', None)
    diag.bozo_exception = str(exc) if exc is not None else None
    diag.entries = len(getattr(parsed, 'entries', []) or [])
    if diag.http_status is None:
        diag.http_status = getattr(parsed, 'status', None)

    diag.verdict = classify_feed(diag.http_status, diag.transport_error, diag.bozo, diag.entries)
    if diag.verdict == 'PARSE_ERROR' and raw:
        diag.suspicious = _scan_suspicious(raw)
    return diag


def format_diagnoses(diagnoses: List[FeedDiagnosis]) -> str:
    """Render a compact table + a detail block for anything not OK."""
    divider = '-' * 78
    lines = ['Feed Doctor — raw output & parse diagnosis', divider,
             f'{"source":16} {"http":>4} {"bytes":>7} {"entries":>7}  verdict', divider]
    for diag in diagnoses:
        status = diag.http_status if diag.http_status is not None else '—'
        lines.append(f'{diag.source_id:16.16} {str(status):>4} {diag.body_bytes:>7} '
                     f'{diag.entries:>7}  {diag.verdict}')
    lines.append(divider)
    problems = [d for d in diagnoses if d.verdict != 'OK']
    if not problems:
        lines.append('all feeds parse cleanly.')
        return '\n'.join(lines)
    for diag in problems:
        lines.append(f'\n[{diag.source_id}] {diag.verdict} — {diag.url}')
        lines.append(f'  content-type: {diag.content_type}')
        if diag.transport_error:
            lines.append(f'  transport: {diag.transport_error}')
        if diag.bozo_exception:
            lines.append(f'  feedparser: {diag.bozo_exception}')
        for finding in diag.suspicious:
            lines.append(f'  suspicious: {finding}')
        lines.append(f'  head: {diag.head[:160]!r}')
    return '\n'.join(lines)
