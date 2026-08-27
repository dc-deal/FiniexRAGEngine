"""The SSE wire format (ISSUE_9) — the renderer both the stream and the published sample use.

This file guards the format itself, byte by byte. It exists at all because the sample and the wire
were rendered by two different functions and drifted apart twice; now there is one renderer, and
these are its rules.
"""
import json

import pytest

from finiexragengine.core.outcome.stream_frames import (
    RETRY_MS,
    render_control,
    render_heartbeat,
    render_retry,
    render_signal,
)
from finiexragengine.types.stream_types import CONTROL_CODES

_ENVELOPE = {'schema_version': '2.0', 'seq': 1041, 'stream_epoch': 1,
             'pipeline_id': 'crypto_sentiment', 'status': 'success',
             'result': [{'symbol': 'BTCUSD', 'signal': 'BUY', 'reasoning': 'a\nb'}]}


def _data(frame: str) -> dict:
    """The payload of a well-formed frame: `event:`, `data:`, and the blank line that dispatches it."""
    lines = frame.split('\n')
    assert lines[0].startswith('event: '), lines[0]
    assert lines[1].startswith('data: '), lines[1]
    assert lines[2:] == ['', ''], f'a frame ends with exactly one blank line, got {lines[2:]!r}'
    return json.loads(lines[1][len('data: '):])


def test_a_frame_is_a_named_event_a_data_line_and_a_blank_line() -> None:
    """The blank line is not cosmetic: per the SSE specification it is what DISPATCHES the event, so
    a frame without it is buffered by a conforming client indefinitely. It read as correct in the
    published sample only because the generator's join supplied it — which is why one shared
    renderer owns this now."""
    frame = render_signal(_ENVELOPE)

    assert frame.startswith('event: signal\ndata: {')
    assert frame.endswith('}\n\n')
    assert frame.count('\n') == 3


def test_no_frame_carries_an_id_line() -> None:
    """An `id:` would make a conforming client send `Last-Event-ID` on reconnect — a header we do
    not honour, since `?since=` is the only cursor. Emitting one invites a client to rely on it."""
    frames = [render_signal(_ENVELOPE),
              render_heartbeat(1, 1041, 1787705462148, available_msc=1787705420265),
              render_control('live', 1, {'head_seq': 1041})]

    for frame in frames:
        assert 'id:' not in frame


def test_a_newline_inside_model_written_text_is_escaped_never_emitted() -> None:
    """`reasoning` is model-written and contains newlines. A literal one would split the frame in
    two — unparseable, and it would shift every following frame by one."""
    frame = render_signal(_ENVELOPE)

    assert '\\n' in frame                                     # escaped inside the JSON string
    assert _data(frame)['result'][0]['reasoning'] == 'a\nb'   # and it round-trips


def test_the_envelope_travels_verbatim() -> None:
    """Frame == stored envelope (§3.2). A projected frame is a place where live and archive can
    drift silently, so the renderer must not add, drop or reorder a key."""
    parsed = _data(render_signal(_ENVELOPE))

    assert parsed == _ENVELOPE
    assert list(parsed) == list(_ENVELOPE)


def test_the_json_is_compact() -> None:
    """13.5 kB per 8-symbol envelope at ~1 MB/day/stream — separator whitespace is not free, and a
    pretty-printed payload is the change this renderer exists to make impossible."""
    assert ', ' not in render_signal(_ENVELOPE)
    assert '": ' not in render_signal(_ENVELOPE)


def test_a_frame_without_the_epoch_is_refused() -> None:
    """The cursor is `(stream_epoch, seq)`, and the rule has no per-frame exception."""
    with pytest.raises(AssertionError, match='stream_epoch'):
        render_signal({'seq': 1041})


def test_an_unknown_control_code_is_refused_at_the_producing_seam() -> None:
    """Strict where a frame is written: a typo must fail here, not reach a consumer as a condition
    they have no handler for. There is no permissive counterpart because a control frame is never
    parsed from an archive — it does not outlive its connection."""
    with pytest.raises(ValueError, match='closed'):
        render_control('epoch_change', 1)          # the plausible typo


@pytest.mark.parametrize('code', CONTROL_CODES)
def test_every_declared_control_code_renders(code: str) -> None:
    assert _data(render_control(code, 1))['code'] == code


def test_the_cold_stream_heartbeat_omits_available_msc_but_keeps_now_msc() -> None:
    """Nothing has been produced, so there is no instant at which anything became fetchable — and
    `now_msc` is then the ONLY liveness signal. `seq: 0` cannot collide with a real seq, because the
    counter returns seq+1."""
    payload = _data(render_heartbeat(1, 0, 1787705462148))

    assert payload == {'stream_epoch': 1, 'seq': 0, 'now_msc': 1787705462148}


def test_the_live_heartbeat_carries_both_stamps_so_skew_is_measurable() -> None:
    payload = _data(render_heartbeat(1, 1043, 1787705462148, available_msc=1787705420265))

    assert payload['now_msc'] - payload['available_msc'] == 41883


def test_the_retry_line_opens_the_stream_and_is_a_default_not_a_mandate() -> None:
    """In-band so a client with no policy of its own has a sane initial delay; the consumer's own
    backoff bounds govern (agreed 2026-08-27)."""
    assert render_retry() == f'retry: {RETRY_MS}\n\n'
    assert render_retry(1000) == 'retry: 1000\n\n'


def test_the_retry_line_is_its_own_block_and_does_not_join_the_first_frame() -> None:
    """Found on the wire, not here — the first version of this test asserted the defect.

    With a single newline the `retry:` field joins the NEXT event's block. A conforming client
    would still apply it, so nothing looks broken; what breaks is agreement with the published
    sample, where `retry: 5000` stands alone — and that sample is the consumer's committed
    parser fixture.
    """
    opening = render_retry() + render_signal(_ENVELOPE)

    blocks = [block for block in opening.split('\n\n') if block]
    assert blocks[0] == f'retry: {RETRY_MS}'
    assert blocks[1].startswith('event: signal')
