"""The SSE wire format for `GET /v1/stream/{pipeline_id}` (ISSUE_9) — one renderer, two callers.

**This module exists because the sample and the wire drifted apart twice.**
`docs/architecture/STREAM_FRAMES_SAMPLE.sse` is what the consumer builds their parser against, and
it was rendered by `experiments/stream_frames_sample/generate.py` with its own private frame
function. Two reissues therefore disagreed with the contract they were meant to demonstrate, and the
consumer found both by parsing them. A published sample rendered by different code than the live
stream is a second implementation of a format that has exactly one specification.

So: a deliberate **function module** (like `provider_factory`, `envelope_contract`) rather than a
class, imported by the router that serves frames *and* by the generator that publishes the sample.
A drift between them is now impossible rather than merely discouraged.

The format, and why each part is the way it is:

* **named events throughout** (`signal`, `heartbeat`, `control`), never the default event. A
  heartbeat is not a comment: SSE comment lines are discarded by conforming clients per spec, so a
  `: ping` cannot carry state — and this one carries `seq`, which is how a consumer tells a healthy
  connection from a stalled producer;
* **no `id:` line.** Emitting one would make a conforming client send `Last-Event-ID` on reconnect,
  a header we do not honour, since `?since=` is the only cursor. A header sent anyway is ignored;
* **one `data:` line of compact JSON.** `reasoning` is model-written text and contains newlines;
  `json.dumps` escapes them inside the string, so the payload holds no literal newline. That is
  asserted here, at the renderer, and not only in the generator — a pretty-printer added later would
  split frames only under load, which is the worst place to find out;
* **`stream_epoch` on every frame**, heartbeat and control included. The consumer's cursor is
  `(stream_epoch, seq)`, and the restore case runs overnight — when only heartbeats carry a number,
  so the epoch would be missing from exactly the frames that would explain a backwards jump.
  Uniform rather than "every frame with a `seq`": a rule with an exception is a rule someone
  violates while believing they are inside it.
"""
import json
from typing import Any, Dict, Optional

from finiexragengine.types.stream_types import CONTROL_CODES

# Initial reconnect delay, in-band at stream open. A default for a client with no policy of its own,
# never authoritative — the consumer's own backoff bounds govern (confirmed with them 2026-08-27).
RETRY_MS = 5000


def render_retry(retry_ms: int = RETRY_MS) -> str:
    """The `retry:` line, emitted once at stream open before any frame — as its own block.

    The trailing blank line matters here for the same reason it does on a frame, and for one
    more: without it the `retry:` field joins the *next* event's block. That parses (a
    conforming client would apply the retry and dispatch the signal), but it does not match the
    published sample, where `retry: 5000` stands alone — and the consumer's committed parser
    fixture IS that sample. A wire that differs from the fixture they built against is the
    divergence this module exists to prevent, whether or not a spec technically permits it.
    """
    return f'retry: {retry_ms}\n\n'


def render_signal(envelope: Dict[str, Any]) -> str:
    """One envelope frame — the stored envelope, verbatim.

    Verbatim is the contract (ISSUE_9 §3.2): a projected frame is a derived view of the stored
    object, i.e. a place where live and archive can drift silently. `Dict[str, Any]` and not the
    Pydantic model on purpose — the dispatcher reads the journal's JSONB column, and re-validating
    into a model only to serialize it again would let a model default rewrite an archived line on
    its way to the wire.
    """
    return _frame('signal', envelope)


def render_heartbeat(stream_epoch: int, seq: int, now_msc: int,
                     available_msc: Optional[int] = None) -> str:
    """The keep-alive: a connection watchdog *and* a liveness proof, never a freshness claim.

    `now_msc` is server time at emission, so the pair `(available_msc, now_msc)` lets a consumer
    measure clock skew between the two hosts — the same class of question the time-base declaration
    exists to settle, since a clock difference nobody can measure eventually gets blamed on the
    wrong system.

    `available_msc` is **absent on a cold stream** (nothing has been produced, so there is no
    instant at which anything became fetchable). `seq: 0` is safe as "nothing yet" by construction
    rather than by convention: the counter returns `seq + 1`, so the first envelope is 1.
    """
    payload: Dict[str, Any] = {'stream_epoch': stream_epoch, 'seq': seq}
    if available_msc is not None:
        payload['available_msc'] = available_msc
    payload['now_msc'] = now_msc
    return _frame('heartbeat', payload)


def render_control(code: str, stream_epoch: int,
                   fields: Optional[Dict[str, Any]] = None) -> str:
    """An out-of-band condition, as a `code` plus whatever that code carries.

    The code is checked against the closed vocabulary here, at the producing seam, so a typo fails
    where it is written rather than reaching a consumer as a condition they have no handler for.
    """
    if code not in CONTROL_CODES:
        raise ValueError(
            f'unknown control code {code!r} — the wire vocabulary is closed: '
            f'{", ".join(CONTROL_CODES)}')
    payload: Dict[str, Any] = {'code': code, 'stream_epoch': stream_epoch}
    payload.update(fields or {})
    return _frame('control', payload)


def _frame(event: str, payload: Dict[str, Any]) -> str:
    """Render one frame, and refuse the two shapes that would corrupt the stream silently."""
    if 'stream_epoch' not in payload:
        raise AssertionError(
            f'{event} frame carries no stream_epoch — the consumer cursor is (stream_epoch, seq) '
            f'and the rule has no per-frame exception (ISSUE_9 §3.5)')
    line = json.dumps(payload, separators=(',', ':'))
    if '\n' in line or '\r' in line:
        # Unreachable through `json.dumps`, which escapes both inside strings — so this guards a
        # FUTURE change (a pretty-printer, a hand-built line), not today's input. A frame split in
        # two is unparseable and, worse, shifts every following frame by one.
        raise AssertionError(f'{event}: literal newline in the data line')
    # The trailing BLANK LINE is what dispatches the event, per the SSE specification — a frame
    # without it is buffered by a conforming client forever. It read as correct in the published
    # sample only because the generator joined its frames with a newline and supplied the blank line
    # by accident, which is precisely the class of divergence this shared module exists to end.
    return f'event: {event}\ndata: {line}\n\n'
