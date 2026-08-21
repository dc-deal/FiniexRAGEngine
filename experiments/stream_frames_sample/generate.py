"""Generate `docs/architecture/STREAM_FRAMES_SAMPLE.sse` — the wire-format reference for ISSUE_9.

The sample is what both sides build their parser against, so it must never disagree with the
contract it demonstrates. Two reissues did, and both were found by the consumer parsing the file
rather than by us. So the contract is encoded here **as data** and asserted before the file is
written: the Tier 1-3 field set, where each field lives, and the per-frame invariants. A drift
between #9's text and this file now fails the generator instead of shipping.

Frames are rendered from real envelopes in the `outcomes` journal; only the fields those rows
predate are injected, and the header states which. Run: python experiments/stream_frames_sample/generate.py
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg

OUT_PATH = 'docs/architecture/STREAM_FRAMES_SAMPLE.sse'
PIPELINE = 'crypto_sentiment'
CADENCE_MS = 600_000                      # M10 — what the pair is translated to
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$')

# --- the contract, as data (ISSUE_9 §4) -------------------------------------------------------
# Tier 1-3 are unconditional on the wire (R15): a stream frame cannot predate a field, so an
# absent one is unreadable rather than old. Order is the serialization order of an envelope.
ENVELOPE_FIELDS: Tuple[str, ...] = (
    'schema_version', 'seq', 'stream_epoch', 'pipeline_id', 'outcome_type', 'data_origin',
    'config_fingerprint', 'prompt_version', 'prompt_id', 'prompt_hash', 'trigger_reason',
    'timestamp', 'available_msc', 'available_msc_resyncs', 'available_msc_max_correction_ms',
    'status', 'result', 'metadata', 'errors')
SCHEMA_VERSION = '2.0'          # major: trigger_reason left metadata, a Tier 3 relocation
ROW_FIELDS: Tuple[str, ...] = (
    'symbol', 'signal', 'sentiment_score', 'confidence', 'reasoning', 'urgency', 'is_breaking',
    'basis')
# Announced but not built: ISSUE_65's episode identity. The sample shows them empty so a consumer
# meets the shape it will get, but the engine is not held to them yet. When ISSUE_65 ships, move
# these two names into ROW_FIELDS above and this check starts enforcing them.
ROW_FIELDS_PENDING: Tuple[str, ...] = ('breaking_episode_id', 'breaking_episode_start')
# Fields that must NOT appear at a stale location — R16 promoted trigger_reason out of metadata,
# and the previous reissue still carried it there while the prose said otherwise.
MISPLACED: Tuple[Tuple[str, str], ...] = (('metadata', 'trigger_reason'),)
# Every frame carrying state carries the epoch (R19), with no per-frame-type exception: the
# consumer's cursor is (stream_epoch, seq), and an exception in a rule is what R16 just deleted.
EPOCH_ON_EVERY_FRAME = True


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000)


def _shift_epoch_ms(env: Dict[str, Any], delta_ms: int) -> None:
    """The int epoch-ms fields move with the ISO ones — otherwise the translation is not uniform.

    `available_msc` and `evidence_as_of` are integers, so the recursive ISO walk below cannot see
    them; missing them would leave an envelope whose evidence looks minutes older than it measured.
    """
    if env.get('available_msc') is not None:
        env['available_msc'] += delta_ms
    for row in env.get('result', []):
        if row.get('evidence_as_of') is not None:
            row['evidence_as_of'] += delta_ms


def _shift(node: Any, delta: timedelta) -> Any:
    """Uniform time translation: every ISO timestamp moves by the same delta, so the
    timestamp -> available_msc -> fetched_at -> evidence_as_of relationships stay as measured."""
    if isinstance(node, dict):
        return {k: _shift(v, delta) for k, v in node.items()}
    if isinstance(node, list):
        return [_shift(v, delta) for v in node]
    if isinstance(node, str) and ISO.match(node):
        return (datetime.fromisoformat(node.replace('Z', '+00:00')) + delta) \
            .astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return node


def _fetch_pair(database_url: str) -> List[Dict[str, Any]]:
    """The two most recent successful passes of one stream — one pipeline_id, never two variants.
    The reissue-1 defect was exactly this query without the pipeline filter."""
    with psycopg.connect(database_url, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("""SELECT envelope FROM outcomes
                       WHERE pipeline_id = %s AND envelope->>'status' = 'success'
                         AND jsonb_array_length(envelope->'result') > 2
                         AND envelope->>'seq' IS NOT NULL      -- ISSUE_9 era only
                       ORDER BY ts DESC LIMIT 2""", (PIPELINE,))
        rows = [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in cur.fetchall()]
    if len(rows) != 2:
        raise SystemExit(f'need two successful {PIPELINE} envelopes, found {len(rows)}')
    return list(reversed(rows))          # oldest first


def _renumber(env: Dict[str, Any], seq: int) -> Dict[str, Any]:
    """Present a real envelope at an illustrative position — the only edit left besides time.

    The contract fields are NOT written here any more: the running engine emits them, and
    `_check_envelope` is run against the row *before* this function touches it, so a green run is
    evidence that the build satisfies ISSUE_9 rather than a rendering of what it should emit. The
    `seq` is renumbered purely so the control-frame examples (a truncated replay, a cursor beyond
    the head) sit at plausible values instead of around 1.
    """
    env['seq'] = seq
    for row in env['result']:
        # ISSUE_65 is not built; the fields are shown empty so a parser meets the shape it will get.
        row.setdefault('breaking_episode_id', '')
        row.setdefault('breaking_episode_start', False)
    return {k: env[k] for k in ENVELOPE_FIELDS if k in env}


def _check_envelope(env: Dict[str, Any]) -> None:
    """The contract check — this is the point of the file (consumer's §2 suggestion)."""
    missing = [f for f in ENVELOPE_FIELDS if f not in env]
    if missing:
        raise AssertionError(f'envelope missing Tier 1-3 fields: {missing}')
    for container, field in MISPLACED:
        if field in env.get(container, {}):
            raise AssertionError(f'{field!r} still present at {container}.{field} — see R16')
    for row in env['result']:
        row_missing = [f for f in ROW_FIELDS if f not in row]
        if row_missing:
            raise AssertionError(f'{row["symbol"]}: row missing {row_missing}')
        # evidence_as_of is the one conditional field: present exactly when evidence exists
        stamps = [_ms(s['fetched_at']) for s in row.get('sources', []) if s.get('fetched_at')]
        if stamps and row.get('evidence_as_of') != max(stamps):
            raise AssertionError(f'{row["symbol"]}: evidence_as_of != max(fetched_at)')
        if not stamps and 'evidence_as_of' in row:
            raise AssertionError(f'{row["symbol"]}: evidence_as_of present without evidence')
        if row.get('evidence_as_of', 0) > env['available_msc']:
            raise AssertionError(f'{row["symbol"]}: evidence newer than the envelope')


def _frame(event: str, payload: Dict[str, Any]) -> str:
    """One SSE frame. A `data:` line must never contain a literal newline — `reasoning` is
    model-written text and does contain them, escaped inside the JSON string."""
    if EPOCH_ON_EVERY_FRAME and 'stream_epoch' not in payload:
        raise AssertionError(f'{event} frame carries no stream_epoch — see R19')
    line = json.dumps(payload, separators=(',', ':'))
    if '\n' in line:
        raise AssertionError(f'{event}: literal newline in the data line')
    return f'event: {event}\ndata: {line}\n'


def build(database_url: str) -> str:
    first, second = _fetch_pair(database_url)
    # The contract check runs on the RAW rows: what the engine actually wrote, before this script
    # touches anything. That is what makes the sample evidence instead of illustration.
    for env in (first, second):
        _check_envelope(env)

    real_gap = _ms(second['timestamp']) - _ms(first['timestamp'])
    delta_ms = CADENCE_MS - real_gap
    second = _shift(second, timedelta(milliseconds=delta_ms))
    _shift_epoch_ms(second, delta_ms)

    a, b = _renumber(first, 1041), _renumber(second, 1042)
    if b['seq'] != a['seq'] + 1:
        raise AssertionError('seq not contiguous')
    if a['pipeline_id'] != b['pipeline_id']:
        raise AssertionError('two pipeline_ids in one series — the reissue-1 defect')

    now_msc = b['available_msc'] + 41_883
    frames = [
        ': --- 1. connect: GET /v1/stream?pipeline=crypto_sentiment  (history defaults to 1) ---\n',
        'retry: 5000\n',
        _frame('signal', a),
        ': --- 2. replay/history done; everything after this frame is live ---\n',
        _frame('control', {'code': 'live', 'stream_epoch': 1, 'head_seq': 1041}),
        ': --- 3. the next scheduled pass of the same stream ---\n',
        _frame('signal', b),
        ': --- 4. keep-alive, every 20 s on every view. now_msc is server time at emission (R17),\n'
        ':        so a consumer can measure clock skew; a stalled seq is a stalled producer. ---\n',
        _frame('heartbeat', {'stream_epoch': 1, 'seq': 1042,
                             'available_msc': b['available_msc'], 'now_msc': now_msc}),
        ': --- 5. control codes, shown together; each occurs on its own ---\n',
        ': 5a  &since=900 - older than replay_window_hours\n',
        _frame('control', {'code': 'replay_truncated', 'stream_epoch': 1, 'requested_since': 900,
                           'oldest_available_seq': 1038, 'window_hours': 24}),
        ': 5b  &since=9001 - ahead of our head (consumer-side store restore)\n',
        _frame('control', {'code': 'cursor_ahead', 'stream_epoch': 1,
                           'requested_since': 9001, 'head_seq': 1042}),
        ': 5c  token revoked mid-stream; the server closes after this frame\n',
        _frame('control', {'code': 'auth_revoked', 'stream_epoch': 1, 'detail': 'token expired'}),
        ': --- 6. cold start (R18): a stream that exists but has never produced an envelope.\n'
        ':        No snapshot frame - there is nothing to snapshot. seq 0 is "nothing yet" and\n'
        ':        can never collide with a real seq (the counter returns seq+1, so the first\n'
        ':        envelope is 1). available_msc is absent for the same reason; now_msc still\n'
        ':        proves the producer is alive. An UNKNOWN pipeline_id is a 404 on connect,\n'
        ':        never an empty stream. ---\n',
        _frame('control', {'code': 'live', 'stream_epoch': 1, 'head_seq': 0}),
        _frame('heartbeat', {'stream_epoch': 1, 'seq': 0, 'now_msc': now_msc}),
    ]
    header = _header(a, b, real_gap)
    return header + '\n' + '\n'.join(frames)


def _header(a: Dict[str, Any], b: Dict[str, Any], real_gap_ms: int) -> str:
    gap_min = (_ms(b['timestamp']) - _ms(a['timestamp'])) / 60_000
    return f""": FiniexRAGEngine - GET /v1/stream - sample frames (ISSUE_9)   [reissue 5, 2026-08-21]
:
: TWO CONSECUTIVE PASSES OF ONE STREAM (`{a['pipeline_id']}`), {gap_min:.0f} min apart, taken from the
: outcomes journal and rendered against the specified frame format. NOT recorded from a running
: stream - none exists yet.
:
: This file is generated by experiments/stream_frames_sample/generate.py, which asserts the Tier
: 1-3 field set AND each field's location before writing. Two earlier reissues disagreed with the
: contract they were meant to demonstrate and the consumer found both by parsing; the check exists
: so that cannot happen a third time.
:
: REISSUE 5 - the two envelopes are REAL OUTPUT OF THE CURRENT BUILD. Nothing is injected any
: more: the engine emits seq, stream_epoch, available_msc, the clamp counters, evidence_as_of,
: data_origin, config_fingerprint and top-level trigger_reason itself. The contract check runs on
: the RAW journal rows, before this script touches them, so a green run is evidence that the build
: satisfies the contract rather than a rendering of what it should emit. Two honest notes:
:   * `trigger_reason` reads `manual` because these were operator-triggered passes (run_cli). A
:     scheduled bar-close pass carries `scheduled`; the field is showing you it works, not lying.
:   * the only remaining edits are the time translation below and renumbering `seq` to 1041/1042,
:     so the control-frame examples sit at plausible values instead of around 1.
: REISSUE 4 - `schema_version` is now **2.0**, a MAJOR bump. Only one change in the group needed
: one: `trigger_reason` moved out of `metadata`, which is a Tier 3 relocation and therefore a
: coordinated break — and your loader gates on the major, so a minor would not have fired it.
: Everything else (seq, stream_epoch, available_msc, evidence_as_of) is purely additive. The two
: clamp counters `available_msc_resyncs` / `available_msc_max_correction_ms` now ride on the
: envelope; 0/0 means this stream's clock has never stepped backwards.
: REISSUE 3 - `trigger_reason` became a TOP-LEVEL scalar (R16), and every frame carrying state
: carries `stream_epoch` (R19), including heartbeat and control. Section 6 added the cold start
: on an empty stream (R18).
: REISSUE 2 fixed reissue 1, which paired the two #42 fan-out VARIANTS of a single pass and
: numbered them as one series. `seq` is per `pipeline_id`; one subscription never interleaves
: variants.
:
: TIME TRANSLATION - the journal available here holds no consecutive M10 pair that also carries
: `fetched_at` (only two manual runs {real_gap_ms/1000:.0f} s apart). Frame 2 is therefore shifted uniformly -
: EVERY timestamp inside it by the same delta - so the pair sits at a realistic cadence while
: `timestamp` -> `available_msc` -> `fetched_at` -> `evidence_as_of` keep exactly the relationships
: that were measured.
:
: NOT INJECTED any more - see REISSUE 5 above. The only fields this script writes are
: `breaking_episode_id` / `breaking_episode_start`, shown EMPTY because ISSUE_65 is not built; they
: are always present once it ships, and empty on a non-breaking row by definition. `breaking_episode_id` and
: `breaking_episode_start` are shown EMPTY: ISSUE_65 is not built; they are always present once it
: ships, and empty on a non-breaking row by definition. Everything else is untouched production
: content.
:
: Frames: one `data:` line of compact JSON, no `id:` line, named events throughout.
"""


def main() -> None:
    url: Optional[str] = os.environ.get('DATABASE_URL')
    if not url:
        raise SystemExit('DATABASE_URL not set')
    text = build(url)
    with open(OUT_PATH, 'w') as handle:
        handle.write(text)
    longest = max(len(line.encode()) for line in text.split('\n'))
    print(f'{OUT_PATH}: {len(text.encode())} B, longest data line {longest} B — contract check passed')


if __name__ == '__main__':
    main()
