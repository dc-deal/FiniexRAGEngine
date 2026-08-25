"""Feed doctor (ISSUE_11) — pull a feed's raw output and diagnose why it fails to parse.

The operator's "get to the error source" tool: it fetches the raw bytes *and* runs the same
feedparser path the ingest worker uses, then classifies the outcome with the exact taxonomy
source-health records — so a red row in the Sources report has a one-command explanation
(e.g. cryptoslate's `not well-formed` is really an HTTP 429 error body, not a broken feed).

Network-touching by design (that is the diagnosis); the pure classifier `classify_feed` is
tested without a network.
"""
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import feedparser

from finiexragengine.core.sources.rss_source import USER_AGENT

# What "fresh" means for a feed that has not said otherwise (ISSUE_107). Seven days: long enough
# that no ordinary news or press feed trips it, short enough that a fossil cannot hide behind it —
# the two cases that motivated this answered 5,520h and 2,637h. A feed with a legitimately slower
# rhythm declares its own via `SourceConfig.expected_max_age_hours` rather than moving this.
DEFAULT_MAX_AGE_HOURS = 168


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
    # OK / RATE_LIMITED / HTTP_ERROR / PARSE_ERROR / UNREACHABLE / EMPTY / STALE
    verdict: str = 'OK'
    suspicious: List[str] = field(default_factory=list)
    # Switched off in config: still probed on purpose — the doctor is the tool that answers
    # "can I turn this back on yet?", so it must reach a feed the worker deliberately skips.
    disabled: bool = False
    # Age of the newest item the feed carries (ISSUE_107), and the threshold it was judged by.
    # `None` age = the feed dated nothing, which is not evidence of staleness and never a verdict.
    newest_age_hours: Optional[float] = None
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS
    # Where that threshold came from: 'declared' (the feed's own `expected_max_age_hours`) or
    # 'default'. Carried so a STALE verdict can name its own basis — a threshold whose origin is
    # invisible is a verdict nobody can check.
    age_basis: str = 'default'


def classify_feed(http_status: Optional[int], transport_error: Optional[str],
                  bozo: bool, entries: int, newest_age_hours: Optional[float] = None,
                  max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> str:
    """The taxonomy verdict — mirrors RssSource so the doctor explains what the worker recorded.

    The last two checks are ISSUE_107's, and they exist because *parsing is not delivering*: a feed
    can answer 200, parse without a complaint and still carry nothing this engine can use. Both
    cases were measured on real candidates while the doctor called them `OK` —
    `blockworks` (50 entries, newest 5,520h old) and binance's announcement RSS (`202`, empty body).

    - `EMPTY` — a 2xx that parses to zero entries. **Threshold-free**, and deliberately so: there
      is no policy to get wrong, and it catches every "endpoint exists but answers nothing" shape.
    - `STALE` — the newest item is older than the feed is allowed to be. The one check that needs a
      threshold, which is why the feed may declare its own.

    Order matters: transport and HTTP failures outrank both, because an unreachable feed's emptiness
    says nothing about the feed.
    """
    if http_status is not None and http_status >= 400:
        return 'RATE_LIMITED' if http_status == 429 else 'HTTP_ERROR'
    if transport_error is not None:
        return 'UNREACHABLE'
    if bozo and not entries:
        return 'PARSE_ERROR'
    if not entries:
        return 'EMPTY'
    # An undated feed is not a stale feed: plenty of valid RSS omits pubDate, and inventing a
    # verdict from a missing field would flag working sources for a formatting choice.
    if newest_age_hours is not None and newest_age_hours > max_age_hours:
        return 'STALE'
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


def _newest_entry_age_hours(entries: List[dict]) -> Optional[float]:
    """Hours since the newest dated entry, or None when the feed dates nothing.

    Reads whichever of `published`/`updated` feedparser managed to parse — a feed that carries only
    one of the two is ordinary, and taking the max over both is what makes an updated-only feed
    (several exchange announcement channels) measurable at all.
    """
    newest: Optional[float] = None
    for entry in entries:
        stamp = (getattr(entry, 'published_parsed', None)
                 or getattr(entry, 'updated_parsed', None))
        if stamp is None:
            continue
        moment = datetime.fromtimestamp(time.mktime(stamp), tz=timezone.utc)
        age = (datetime.now(timezone.utc) - moment).total_seconds() / 3600.0
        newest = age if newest is None else min(newest, age)
    return newest


def diagnose_feed(source_id: str, url: str, *, timeout: int = 20,
                  disabled: bool = False,
                  expected_max_age_hours: Optional[int] = None) -> FeedDiagnosis:
    """Raw GET + feedparser parse + classification for one feed."""
    diag = FeedDiagnosis(source_id=source_id, url=url, disabled=disabled)
    # The feed's own expectation wins where it declares one (ISSUE_107); the basis travels with it
    # so the report can say which number it judged against.
    if expected_max_age_hours is not None:
        diag.max_age_hours = expected_max_age_hours
        diag.age_basis = 'declared'
    raw = b''
    # 1. Raw fetch — this is where the true HTTP status (e.g. 429) is visible before feedparser
    #    ever tries to treat the body as XML.
    try:
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
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

    # 2. Parse through feedparser (the worker's path) for bozo / entries — same agent as the
    #    worker, so the diagnosis cannot pass where the real fetch would 403 (or vice versa).
    parsed = feedparser.parse(url, agent=USER_AGENT)
    diag.bozo = bool(getattr(parsed, 'bozo', 0))
    exc = getattr(parsed, 'bozo_exception', None)
    diag.bozo_exception = str(exc) if exc is not None else None
    entries = list(getattr(parsed, 'entries', []) or [])
    diag.entries = len(entries)
    diag.newest_age_hours = _newest_entry_age_hours(entries)
    if diag.http_status is None:
        diag.http_status = getattr(parsed, 'status', None)

    diag.verdict = classify_feed(diag.http_status, diag.transport_error, diag.bozo, diag.entries,
                                 newest_age_hours=diag.newest_age_hours,
                                 max_age_hours=diag.max_age_hours)
    if diag.verdict == 'PARSE_ERROR' and raw:
        diag.suspicious = _scan_suspicious(raw)
    return diag


# One line per verdict, rendered only for the verdicts a run actually produced (ISSUE_107). A
# legend that always lists everything is noise on a healthy fleet; a legend that appears exactly
# when a state does is the state's own explanation, and it says which rule fired.
_LEGEND = {
    'RATE_LIMITED': 'the host answered 429 — back off, the feed itself is fine',
    'HTTP_ERROR': 'the host refused (4xx/5xx) — check the URL, then whether this egress IP is walled',
    'UNREACHABLE': 'no answer at all (DNS, TLS, timeout) — transport, not content',
    'PARSE_ERROR': 'a body arrived and is not parseable RSS — see the head dump below',
    'EMPTY': 'a 2xx that parses to zero entries — the endpoint exists and delivers nothing '
             '(threshold-free)',
    'STALE': 'the newest item is older than this feed is allowed to be — parses perfectly, '
             'publishes nothing',
}


def _age_cell(diag: FeedDiagnosis) -> str:
    """The newest item's age, compact — '—' when the feed dates nothing (not a fault)."""
    if diag.newest_age_hours is None:
        return '—'
    if diag.newest_age_hours < 48:
        return f'{diag.newest_age_hours:.0f}h'
    return f'{diag.newest_age_hours / 24:.0f}d'


def format_diagnoses(diagnoses: List[FeedDiagnosis], *,
                     elapsed_seconds: Optional[float] = None,
                     workers: int = 1) -> str:
    """Render a compact table + a census, a legend for the states present, and a detail block."""
    divider = '-' * 78
    lines = ['Feed Doctor — raw output & parse diagnosis', divider,
             f'{"source":16} {"http":>4} {"bytes":>7} {"entries":>7} {"age":>6}  verdict', divider]
    for diag in diagnoses:
        status = diag.http_status if diag.http_status is not None else '—'
        # A green but switched-off feed would otherwise read as "working" while the worker
        # never polls it — the marker is what makes the re-enable decision obvious.
        verdict = f'{diag.verdict} [disabled]' if diag.disabled else diag.verdict
        # A STALE row names the number it lost to and where that number came from, inline: the
        # verdict is only checkable if its basis is visible next to it.
        if diag.verdict == 'STALE':
            verdict += f' (> {diag.max_age_hours}h · {diag.age_basis})'
        lines.append(f'{diag.source_id:16.16} {str(status):>4} {diag.body_bytes:>7} '
                     f'{diag.entries:>7} {_age_cell(diag):>6}  {verdict}')
    lines.append(divider)

    # The census — so "nothing was reported" is distinguishable from "nothing was checked".
    counts: dict = {}
    for diag in diagnoses:
        counts[diag.verdict] = counts.get(diag.verdict, 0) + 1
    census = ' · '.join(f'{count} {verdict}' for verdict, count in sorted(counts.items()))
    disabled = sum(1 for d in diagnoses if d.disabled)
    lines.append(f'{len(diagnoses)} probed · {census} · {disabled} disabled')
    # Say what the wait was spent on. The doctor makes TWO requests per feed (a raw GET for the
    # true HTTP status, then feedparser's own fetch on the worker's exact path), and a walled or
    # silent host burns its full timeout on both — so the cost is dominated by the failures, not
    # by the feed count. Without this line a two-minute run looks like a hang (ISSUE_107: the
    # catalogue went from 14 feeds to 39, and the run time went with it).
    if elapsed_seconds is not None:
        slow = sorted((d for d in diagnoses if d.verdict != 'OK'), key=lambda d: d.source_id)
        detail = f'{len(diagnoses) * 2} requests, {workers} at a time' if workers > 1 else \
                 f'{len(diagnoses) * 2} requests, one at a time'
        line = f'took {elapsed_seconds:.1f}s · {detail}'
        if slow:
            line += (f' · {len(slow)} feed(s) burnt a timeout or were refused '
                     f'({", ".join(d.source_id for d in slow)})')
        lines.append(line)
    # And where the staleness gate came from, named per feed that overrides it.
    declared = [d for d in diagnoses if d.age_basis == 'declared']
    gate = f'staleness gate: {DEFAULT_MAX_AGE_HOURS}h default'
    if declared:
        own = ', '.join(f'{d.source_id} {d.max_age_hours}h' for d in declared)
        gate += f' · {len(declared)} feed(s) declare their own ({own})'
    lines.append(gate)

    problems = [d for d in diagnoses if d.verdict != 'OK']
    if not problems:
        lines.append(divider)
        lines.append('all feeds parse cleanly and carry recent items.')
        return '\n'.join(lines)

    lines.append(divider)
    for verdict in sorted({d.verdict for d in problems}):
        if verdict in _LEGEND:
            lines.append(f'{verdict}: {_LEGEND[verdict]}')
    for diag in problems:
        lines.append(f'\n[{diag.source_id}] {diag.verdict} — {diag.url}')
        lines.append(f'  content-type: {diag.content_type}')
        if diag.transport_error:
            lines.append(f'  transport: {diag.transport_error}')
        if diag.bozo_exception:
            lines.append(f'  feedparser: {diag.bozo_exception}')
        if diag.verdict == 'STALE':
            lines.append(f'  newest item: {_age_cell(diag)} old, allowed {diag.max_age_hours}h '
                         f'({diag.age_basis})')
        for finding in diag.suspicious:
            lines.append(f'  suspicious: {finding}')
        lines.append(f'  head: {diag.head[:160]!r}')
    return '\n'.join(lines)
